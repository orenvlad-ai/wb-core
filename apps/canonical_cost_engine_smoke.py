"""Targeted invariants for the unified canonical cost engine and baseline."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.canonical_cost_engine import (  # noqa: E402
    BASELINE_ONEC,
    CanonicalCostBlocked,
    CanonicalCostEngine,
    allocate_partial_payment,
    reconcile_outstanding_layers,
    roll_wac,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
    _serialize_sheet_vitrina_plan,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


def main() -> int:
    _partial_payment()
    _wac_and_snapshot_stability()
    _outstanding_reconciliation()
    _baseline_and_physical_sources()
    print("canonical_cost_engine_smoke: ok")
    return 0


def _partial_payment() -> None:
    rows = allocate_partial_payment(
        [
            {"nm_id": 1, "qty": 60, "invoice_value": 600},
            {"nm_id": 2, "qty": 40, "invoice_value": 400},
        ],
        paid_share="0.15",
        paid_rub="1500",
    )
    _eq(rows[0]["paid_equivalent_quantity"], Decimal("9"), "15% first SKU")
    _eq(rows[1]["paid_equivalent_quantity"], Decimal("6"), "15% second SKU")
    _eq(sum((row["paid_capital_rub"] for row in rows), Decimal("0")), Decimal("1500"), "payment allocation")


def _wac_and_snapshot_stability() -> None:
    qty, capital, wac = roll_wac(quantity=0, capital=0, receipt_quantity=100, receipt_unit_cost=10)
    _eq(wac, Decimal("10"), "first receipt WAC")
    qty, capital, wac = roll_wac(
        quantity=qty, capital=capital, receipt_quantity=100, receipt_unit_cost=20
    )
    _eq(wac, Decimal("15"), "two receipt WAC")
    debit_snapshot = wac
    qty, capital, wac_after = roll_wac(
        quantity=qty, capital=capital, writeoff_quantity=50
    )
    _eq(wac_after, Decimal("15"), "ordinary writeoff preserves remaining WAC")
    qty, capital, newer_wac = roll_wac(
        quantity=qty, capital=capital, receipt_quantity=50, receipt_unit_cost=30
    )
    _eq(debit_snapshot, Decimal("15"), "older WB supply snapshot is immutable")
    if newer_wac == debit_snapshot:
        raise AssertionError("newer FF receipt must update current WAC")


def _outstanding_reconciliation() -> None:
    layers = [
        _layer("s1", "2026-07-02", 10, 100),
        _layer("s2", "2026-07-03", 10, 200),
    ]
    after = reconcile_outstanding_layers(
        layers,
        [
            _doprinato("d1", "2026-07-04", 6, original="s1"),
            _doprinato("d2", "2026-07-05", 4, original="s1"),
            _doprinato("d3", "2026-07-06", 5),
        ],
    )
    _eq(Decimal(after[0]["open_quantity"]), Decimal("0"), "sent100 accepted90 +6 +4")
    _eq(Decimal(after[1]["open_quantity"]), Decimal("5"), "strict FIFO keeps exact second layer")
    first_capital = Decimal(after[0]["open_quantity"]) * Decimal(after[0]["recognized_unit_cost_rub"])
    second_capital = Decimal(after[1]["open_quantity"]) * Decimal(after[1]["recognized_unit_cost_rub"])
    _eq(first_capital + second_capital, Decimal("1000"), "outstanding weighted layer capital")
    repeated = reconcile_outstanding_layers(layers, [_doprinato("same", "2026-07-04", 6), _doprinato("same", "2026-07-04", 6)])
    _eq(Decimal(repeated[0]["open_quantity"]), Decimal("4"), "repeat is idempotent")
    try:
        reconcile_outstanding_layers(layers, [_doprinato("bad", "2026-07-04", 21)])
    except CanonicalCostBlocked as exc:
        if exc.code != "doprinato_unmatched_surplus":
            raise
    else:
        raise AssertionError("over-doprinato must fail closed")
    future = [_layer("future", "2026-07-10", 10, 300)]
    try:
        reconcile_outstanding_layers(future, [_doprinato("early", "2026-07-09", 1)])
    except CanonicalCostBlocked:
        pass
    else:
        raise AssertionError("future outstanding must not be a FIFO candidate")


def _baseline_and_physical_sources() -> None:
    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            _insert_primary(conn)
            _insert_fallback_production(conn, nm_id=222)
            _insert_supplier_payment(
                conn, shipment_id="fallback-production", cny="150", rub="1500"
            )
            _insert_ff_balance(conn, nm_id=111, quantity=6750)
            _insert_snapshot(conn, "2026-05-16", {222: {"onec_FF_STOCK_unit_cost_rub": 80}})
            _insert_snapshot(conn, "2026-05-17", {222: {"onec_FF_STOCK_unit_cost_rub": 90}})
            _insert_snapshot(conn, "2026-07-01", {111: {"stock_total": 93250}, 222: {"stock_total": 0}})
            conn.commit()
        engine = CanonicalCostEngine(runtime=runtime, timestamp_factory=lambda: "2026-07-12T00:00:00Z")
        primary = engine.discover_primary_baseline_shipment()
        _eq(primary["shipment_id"], "primary-june", "primary discovery")
        _eq(Decimal(primary["weighted_ff_unit_cost_rub"]), Decimal("111.181389"), "expected FF average")
        plan = engine.build_baseline_plan()
        _eq(plan["primary_sku_count"], 1, "primary SKU count")
        _eq(plan["fallback_sku_count"], 1, "fallback SKU count")
        fallback = plan["fallbacks"][0]
        _eq(fallback["as_of_date"], "2026-05-16", "nearest allowed 1C date")
        _eq(fallback["source_type"], BASELINE_ONEC, "1C quality provenance")
        _eq(fallback["unit_cost_rub"], "80", "post-cutoff 1C is forbidden")
        if "near_future_proxy" in str(plan):
            raise AssertionError("future proxy is forbidden")
        _eq(plan["physical"]["111"]["FF"], "6750", "FF physical quantity comes from ledger")
        _eq(plan["cost_coverage"], "1", "baseline coverage 100%")
        production = next(
            line for line in plan["lines"]
            if line["nm_id"] == 222 and line["stage"] == "PRODUCTION"
        )
        _eq(production["physical_quantity"], "100", "full production quantity")
        _eq(
            production["paid_equivalent_quantity"], "15",
            "15% payment is allocated over the full production line set",
        )
        _eq(production["paid_capital_rub"], "1500", "factual paid capital")
        engine.materialize_baseline_plan(plan)
        result = engine.rebuild(date_from="2026-07-01", date_to="2026-07-01")
        if result.daily_rows_changed <= 0:
            raise AssertionError("first unified projection must materialize rows")
        second = engine.rebuild(date_from="2026-07-01", date_to="2026-07-01")
        _eq(second.daily_rows_changed, 0, "repeat daily materialization")
        with _connect(runtime.db_path) as conn:
            ff = conn.execute(
                "SELECT physical_quantity FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date='2026-07-01' AND nm_id=111 AND stage='FF'"
            ).fetchone()
            wb = conn.execute(
                "SELECT physical_quantity FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date='2026-07-01' AND nm_id=111 AND stage='WB'"
            ).fetchone()
        _eq(ff[0], "6750", "daily FF/ledger reconciliation")
        _eq(wb[0], "93250", "WB physical quantity comes from official stock")
        _canonical_outstanding_sql(engine, runtime)
        with _connect(runtime.db_path) as conn:
            _insert_fallback_production(conn, nm_id=333, shipment_id="missing-cost")
            conn.commit()
        try:
            engine.build_baseline_plan()
        except CanonicalCostBlocked as exc:
            if exc.code != "baseline_cost_coverage_incomplete":
                raise
        else:
            raise AssertionError("missing opening SKU cost must block the whole baseline")


def _canonical_outstanding_sql(
    engine: CanonicalCostEngine, runtime: RegistryUploadDbBackedRuntime
) -> None:
    changed = engine._replace_versioned_movement_plans(  # noqa: SLF001 - targeted persistence smoke
        [
            {
                "operation_id": "wb-debit-1", "supply_id": "wb-supply-1", "nm_id": 111,
                "effective_date": "2026-07-02", "sent_quantity": "100",
                "paid_equivalent_quantity": "100", "cost_coverage_share": "1",
                "confirmation_share": "1", "recognized_unit_cost_rub": "120",
                "paid_unit_cost_rub": "110", "recognized_capital_rub": "12000",
                "paid_capital_rub": "11000", "ff_wac_quantity_before": "6750",
                "source_operation_key": "wb:supply:1",
            }
        ]
    )
    _eq(changed, 1, "movement snapshot persisted")
    with _connect(runtime.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                operation_id,operation_type,source_type,source_key,source_object_id,
                source_object_label,created_at,created_by,sku_count,total_quantity_delta,
                total_quantity_abs,warnings_json,diagnostics_json
            ) VALUES('wb-debit-1','auto_writeoff','wb_supply','wb:supply:1',
                     'wb-supply-1','WB supply 1','2026-07-02T00:00:00Z','fixture',
                     1,-100,100,'[]','{}')
            """
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                operation_id,line_no,nm_id,quantity_delta,raw_json
            ) VALUES('wb-debit-1',1,111,-100,'{}')
            """
        )
        _insert_wb_supply(conn, "wb-supply-1", 90, "2026-07-03")
        conn.commit()
    engine._materialize_outstanding_layers("2026-07-03")  # noqa: SLF001
    _eq(_open_qty(runtime), "10", "accepted 90 leaves outstanding 10")
    with _connect(runtime.db_path) as conn:
        _insert_wb_supply(conn, "dop-1", 6, "2026-07-04", doprinato=True)
        conn.commit()
    engine._materialize_outstanding_layers("2026-07-04")  # noqa: SLF001
    _eq(_open_qty(runtime), "4", "doprinato 6 leaves outstanding 4")
    with _connect(runtime.db_path) as conn:
        _insert_wb_supply(conn, "dop-2", 4, "2026-07-05", doprinato=True)
        conn.commit()
    engine._materialize_outstanding_layers("2026-07-05")  # noqa: SLF001
    _eq(_open_qty(runtime), "0", "doprinato 4 closes outstanding")
    _eq(
        engine.physical_quantities_as_of("2026-07-05")[111]["FF_TO_WB"],
        Decimal("0"),
        "doprinato closes the physical FF-to-WB substate without another FF debit",
    )
    with _connect(runtime.db_path) as conn:
        _insert_snapshot(conn, "2026-07-03", {111: {"stock_total": 93340}})
        _insert_snapshot(conn, "2026-07-04", {111: {"stock_total": 93346}})
        _insert_snapshot(conn, "2026-07-05", {111: {"stock_total": 93350}})
        _insert_snapshot(conn, "2026-07-06", {111: {"stock_total": 90000}})
        _insert_snapshot(conn, "2026-07-07", {111: {"stock_total": 90010}})
        conn.commit()
    states = engine._wb_cost_states(  # noqa: SLF001 - rolling-state invariant smoke
        ["2026-07-01", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"]
    )
    opening_capital = Decimal("93250") * Decimal("111.181389")
    _eq(
        Decimal(states["2026-07-03"][111]["recognized_capital"]),
        opening_capital + Decimal("90") * Decimal("120"),
        "original acceptance enters WB with debit snapshot cost",
    )
    _eq(
        Decimal(states["2026-07-05"][111]["recognized_capital"]),
        opening_capital + Decimal("100") * Decimal("120"),
        "doprinato 6+4 enters WB with the original layer",
    )
    wac_before_reduction = Decimal(states["2026-07-05"][111]["recognized_capital"]) / Decimal("93350")
    wac_after_reduction = Decimal(states["2026-07-06"][111]["recognized_capital"]) / Decimal("90000")
    _eq(wac_after_reduction, wac_before_reduction, "WB stock reduction preserves WAC")
    growth = states["2026-07-07"][111]
    _eq(growth["quality"], "unexplained_growth_existing_wac", "unexplained growth is explicit")
    _eq(
        Decimal(growth["recognized_capital"]) / Decimal("90010"),
        wac_after_reduction,
        "unexplained growth uses only the existing WAC estimate",
    )
    import json
    with _connect(runtime.db_path) as conn:
        conn.execute(
            "UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='wb-supply-1'",
            (json.dumps([{"nmID": 111, "acceptedQuantity": 101, "quantity": 101}]),),
        )
        conn.commit()
    try:
        engine.physical_quantities_as_of("2026-07-07")
    except CanonicalCostBlocked as exc:
        _eq(exc.code, "accepted_quantity_exceeds_sent", "ordinary over-acceptance code")
    else:
        raise AssertionError("ordinary accepted quantity above sent must fail closed")


def _insert_wb_supply(
    conn, supply_id: str, accepted: int, fact_date: str, *, doprinato: bool = False
) -> None:
    normalized = {
        "supply_id": supply_id, "status_id": 5, "fact_date": fact_date,
        "warehouse_name": "W", "destination_name": "D",
        "virtual_type_id": 5 if doprinato else 0,
        "type_label": "Допринято" if doprinato else "Обычная",
    }
    import json
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_supplies(
            supply_id,cache_key,normalized_row_json,raw_goods_json,warehouse_id,status_id,
            quantity_for_size_filter,fact_date,synced_at
        ) VALUES(?,?,?,?,?,5,?,?,?)
        """,
        (
            supply_id, f"cache-{supply_id}", json.dumps(normalized, ensure_ascii=False),
            json.dumps([{"nmID": 111, "acceptedQuantity": accepted, "quantity": accepted}], ensure_ascii=False),
            "W", accepted, fact_date, f"{fact_date}T12:00:00Z",
        ),
    )


def _open_qty(runtime: RegistryUploadDbBackedRuntime) -> str:
    with _connect(runtime.db_path) as conn:
        row = conn.execute(
            "SELECT open_quantity FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1 AND original_supply_id='wb-supply-1'"
        ).fetchone()
    return str(row[0])


def _insert_primary(conn) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipments(
            shipment_id,created_at,updated_at,shipment_date,actual_shipment_date,
            actual_ff_acceptance_date,order_status,expenses_complete,invoice_no,invoice_date,
            currency,product_qty_total,product_amount_total,extras_amount_total,invoice_amount_total,
            match_status,warnings_json,errors_json
        ) VALUES('primary-june','2026-06-01T00:00:00Z','2026-06-24T00:00:00Z','2026-06-01',
                 '2026-06-10','2026-06-23','accepted_ff',1,'INV-1','2026-06-01','CNY',
                 100000,1000000,0,1000000,'all_matched','[]','[]')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
            line_id,shipment_id,line_type,sort_order,internal_sku,internal_nm_id,internal_name,
            qty,unit_price,amount,currency,match_status,manual_override,raw_json
        ) VALUES('line-primary','primary-june','product',1,'SKU-111',111,'SKU 111',100000,10,1000000,
                 'CNY','matched',0,'{}')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layers(
            layer_id,supplier_shipment_id,status,accepted_ff_date,calculated_at,effective_cny_rate,
            invoice_amount_total_cny,invoice_extras_total_cny,product_qty_total,common_expense_pool_rub,
            common_expense_per_unit_rub,weighted_avg_ff_unit_cost_rub,reconciliation_status,
            reconciliation_delta_rub,inputs_hash,version,is_current,source_status_json,component_status_json
        ) VALUES('ff-primary','primary-june','confirmed','2026-06-23','2026-06-24T00:00:00Z',10,
                 1000000,0,100000,1118138.9,11.181389,111.181389,'ok',0,'primary-hash',1,1,'{}','{}')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layer_lines(
            layer_line_id,layer_id,supplier_shipment_id,supplier_line_id,nm_id,sku,display_name,qty,
            invoice_unit_price_cny,sku_purchase_cost_rub,allocated_common_expenses_per_unit_rub,
            sku_ff_unit_cost_rub,line_total_cost_rub,allocation_method,source_status
        ) VALUES('ff-line-primary','ff-primary','primary-june','line-primary',111,'SKU-111','SKU 111',
                 100000,10,100,11.181389,111.181389,11118138.9,'qty_based_common_pool','confirmed')
        """
    )


def _insert_fallback_production(conn, *, nm_id: int, shipment_id: str = "fallback-production") -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipments(
            shipment_id,created_at,updated_at,shipment_date,order_status,expenses_complete,
            invoice_no,invoice_date,currency,product_qty_total,product_amount_total,extras_amount_total,
            invoice_amount_total,match_status,warnings_json,errors_json
        ) VALUES(?,?,?,?, 'production',0,?,?, 'CNY',100,1000,0,1000,'all_matched','[]','[]')
        """,
        (shipment_id, "2026-06-25T00:00:00Z", "2026-06-25T00:00:00Z", "2026-06-25", shipment_id, "2026-06-25"),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
            line_id,shipment_id,line_type,sort_order,internal_sku,internal_nm_id,internal_name,
            qty,unit_price,amount,currency,match_status,manual_override,raw_json
        ) VALUES(?,?, 'product',1,?,?,?,100,10,1000,'CNY','matched',0,'{}')
        """,
        (f"line-{shipment_id}", shipment_id, f"SKU-{nm_id}", nm_id, f"SKU {nm_id}"),
    )


def _insert_ff_balance(conn, *, nm_id: int, quantity: int) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,source_object_id,source_object_label,
            created_at,created_by,sku_count,total_quantity_delta,total_quantity_abs,warnings_json,diagnostics_json
        ) VALUES('opening-ff','opening_balance','manual','opening-ff','opening-ff','opening',
                 '2026-07-01T00:00:00Z','fixture',1,?,?, '[]','{}')
        """,
        (quantity, quantity),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id,line_no,nm_id,quantity_delta,raw_json
        ) VALUES('opening-ff',1,?,?,'{}')
        """,
        (nm_id, quantity),
    )


def _insert_supplier_payment(
    conn, *, shipment_id: str, cny: str, rub: str
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_cny_ledger_operations(
            operation_id,operation_type,source_document_id,source_order_id,
            operation_date,operation_datetime,sequence_key,cny_delta,rub_value_delta,
            status,error_reason,created_at,updated_at
        ) VALUES(?, 'supplier_payment_out', ?, ?, '2026-06-30',
                 '2026-06-30T00:00:00Z', ?, ?, ?, 'posted', '',
                 '2026-06-30T00:00:00Z', '2026-06-30T00:00:00Z')
        """,
        (
            f"payment-{shipment_id}", f"document-{shipment_id}", shipment_id,
            f"20260630:{shipment_id}", f"-{cny}", f"-{rub}",
        ),
    )


def _insert_snapshot(conn, day: str, values: dict[int, dict[str, float]]) -> None:
    rows = []
    for nm_id, metrics in values.items():
        for metric_key, value in metrics.items():
            rows.append([f"{nm_id} {metric_key}", f"SKU:{nm_id}|{metric_key}", value])
    plan = SheetVitrinaV1Envelope(
        plan_version="canonical-cost-fixture",
        snapshot_id=f"snapshot-{day}",
        as_of_date=day,
        date_columns=[day],
        temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key="day", slot_label="day", column_date=day)],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA", write_start_cell="A1", write_rect=f"A1:C{len(rows)+1}",
                clear_range="A:Z", write_mode="overwrite", partial_update_allowed=False,
                header=["label", "key", day], rows=rows, row_count=len(rows), column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS", write_start_cell="A1", write_rect="A1:B1",
                clear_range="A:B", write_mode="overwrite", partial_update_allowed=False,
                header=["key", "value"], rows=[], row_count=0, column_count=2,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO registry_upload_versions(bundle_version,uploaded_at,activated_at) VALUES(?,?,?)",
        (f"bundle-{day}", f"{day}T00:00:00Z", f"{day}T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ready_snapshots(
            bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (f"bundle-{day}", f"{day}T00:00:00Z", day, plan.snapshot_id, plan.plan_version, f"{day}T01:00:00Z", _serialize_sheet_vitrina_plan(plan)),
    )


def _layer(supply_id: str, accepted_date: str, qty: int, cost: int) -> dict[str, object]:
    return {
        "original_supply_id": supply_id, "nm_id": 1, "warehouse": "W", "destination": "D",
        "open_quantity": str(qty), "accepted_date": accepted_date, "writeoff_date": accepted_date,
        "recognized_unit_cost_rub": str(cost), "paid_unit_cost_rub": str(cost), "provenance": {},
    }


def _doprinato(supply_id: str, accepted_date: str, qty: int, *, original: str = "") -> dict[str, object]:
    return {
        "supply_id": supply_id, "nm_id": 1, "warehouse": "W", "destination": "D",
        "accepted_quantity": str(qty), "accepted_date": accepted_date, "original_supply_id": original,
    }


def _eq(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
