#!/usr/bin/env python3
"""End-to-end contract smoke for the independent own-FBS order planner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_sales_history import (
    persist_sales_history_result_exact_dates,
)
from packages.application.fbs_fulfillment_order import FbsFulfillmentOrderBlock
from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
from packages.application.ff_pool_cutover import MANIFESTS_TABLE
from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.simple_xlsx import read_first_sheet_rows
from packages.application.supplier_shipments import SupplierShipmentsBlock
from packages.application.warehouse_functional import ensure_warehouse_functional_schema
from packages.contracts.sales_funnel_history_block import (
    SalesFunnelHistoryItem,
    SalesFunnelHistorySuccess,
)
from packages.contracts.supplier_shipments import (
    ORDER_STATUS_ACCEPTED_FF,
    ORDER_STATUS_IN_TRANSIT,
    ORDER_STATUS_PRODUCTION,
)


INPUT_BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-04-18T09:00:00Z"
MOSCOW_ID = "ff-moscow"
ORENBURG_ID = "ff-orenburg"


def main() -> int:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="fbs-fulfillment-order-") as raw:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw) / "runtime")
        runtime.ingest_bundle(bundle, activated_at=NOW_TEXT)
        active_nm_ids = [
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled
        ]
        assert len(active_nm_ids) >= 2
        _seed_facilities(runtime, active_nm_ids)
        _seed_sales_history(runtime, active_nm_ids)
        _assert_target_facility_validation(runtime)
        _seed_shipments(runtime, active_nm_ids)

        block = FbsFulfillmentOrderBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
            timestamp_factory=lambda: NOW_TEXT,
        )
        status = block.build_status()
        wb_state = InventoryPlanningReadModel(db_path=runtime.db_path).current()["wb"]
        assert wb_state["aggregate_only"] is True
        assert wb_state["raw_total"] > 100_000
        facilities = {item["facility_id"]: item for item in status.facilities}
        assert status.wb_stock_used is False
        assert facilities[MOSCOW_ID]["calculation_enabled"] is True
        assert facilities[MOSCOW_ID]["physical"] == 100 + 200 * (len(active_nm_ids) - 1)
        assert facilities[MOSCOW_ID]["reserved"] == 30
        assert facilities[MOSCOW_ID]["available"] == (
            100 + 200 * (len(active_nm_ids) - 1) - 30
        )
        assert facilities[MOSCOW_ID]["remaining_active_inbound_qty"] == 15
        assert all(
            "seller_stock" not in item
            for item in facilities[MOSCOW_ID]["sku_values"]
        )
        assert facilities[ORENBURG_ID]["calculation_enabled"] is False
        assert facilities[ORENBURG_ID]["physical"] is None
        assert "physical ledger" in " ".join(facilities[ORENBURG_ID]["blockers"])

        last_n = block.calculate(
            {
                "target_facility_id": MOSCOW_ID,
                "production_days": 10,
                "factory_to_target_ff_days": 5,
                "ff_safety_days": 3,
                "order_cycle_days": 2,
                "order_batch_qty": 50,
                "sales_history_mode": "last_n_days",
                "sales_avg_period_days": 14,
            }
        )
        assert last_n.horizon_days == 20
        assert last_n.sales_window["actual_date_from"] == "2026-04-04"
        assert last_n.sales_window["actual_date_to"] == "2026-04-17"
        assert last_n.sales_window["calendar_day_count"] == 14
        assert last_n.sales_window["outside_window_samples_used"] is False
        rows = {row.nm_id: row for row in last_n.rows}
        first = rows[active_nm_ids[0]]
        second = rows[active_nm_ids[1]]
        assert first.selected_facility_physical_fbs == 100
        assert first.selected_facility_reserved_fbs == 30
        assert first.selected_facility_available_fbs == 70
        assert first.remaining_active_inbound_qty == 15
        assert second.remaining_active_inbound_qty == 0
        assert first.coverage_qty == 85
        assert first.recommended_order_qty == math.ceil(
            max(first.national_daily_demand * 20 - 85, 0) / 50
        ) * 50
        assert last_n.wb_stock_used is False
        assert "wb" not in last_n.inbound_coverage
        assert last_n.inbound_coverage["unassigned_target_excluded_count"] == 1
        assert last_n.inbound_coverage["legacy_null_target_fallback_moscow_count"] == 0

        custom = block.calculate(
            {
                "target_facility_id": MOSCOW_ID,
                "production_days": 10,
                "factory_to_target_ff_days": 5,
                "ff_safety_days": 3,
                "order_cycle_days": 2,
                "order_batch_qty": 50,
                "sales_history_mode": "custom_period",
                "sales_date_from": "2026-04-10",
                "sales_date_to": "2026-04-12",
            }
        )
        custom_row = {row.nm_id: row for row in custom.rows}[active_nm_ids[0]]
        assert custom.sales_window["actual_date_from"] == "2026-04-10"
        assert custom.sales_window["actual_date_to"] == "2026-04-12"
        assert custom.summary.sales_calendar_day_count == 3
        assert custom_row.used_trading_day_count == 2
        assert custom_row.excluded_sales_dates == ("2026-04-11",)
        assert custom_row.national_daily_demand == 10
        assert "2026-04-09" not in custom_row.included_sales_dates
        assert "2026-04-13" not in custom_row.included_sales_dates

        _expect_error(
            block,
            {"target_facility_id": ORENBURG_ID},
            "заблокирован",
        )
        for invalid_settings, expected in (
            ({"production_days": 0}, "больше нуля"),
            ({"ff_safety_days": -1}, "не может быть отрицательным"),
            ({"order_batch_qty": 0}, "больше нуля"),
            ({"sales_avg_period_days": 0}, "больше нуля"),
        ):
            _expect_error(
                block,
                {"target_facility_id": MOSCOW_ID, **invalid_settings},
                expected,
            )
        _expect_error(
            block,
            {
                "target_facility_id": MOSCOW_ID,
                "sales_history_mode": "custom_period",
                "sales_date_from": "2026-04-10",
            },
            "обязательны",
        )
        _expect_error(
            block,
            {
                "target_facility_id": MOSCOW_ID,
                "sales_history_mode": "custom_period",
                "sales_date_from": "2026-04-12",
                "sales_date_to": "2026-04-10",
            },
            "позже",
        )
        _expect_error(
            block,
            {
                "target_facility_id": MOSCOW_ID,
                "sales_history_mode": "custom_period",
                "sales_date_from": "2026-04-19",
                "sales_date_to": "2026-04-20",
            },
            "раньше даты расчёта",
        )
        _expect_error(
            block,
            {
                "target_facility_id": MOSCOW_ID,
                "sales_history_mode": "custom_period",
                "sales_date_from": "2026-02-01",
                "sales_date_to": "2026-02-02",
            },
            "authoritative sales history source",
        )

        registry = runtime.list_supply_calculation_registry(
            calculation_type="fbs_fulfillment_order"
        )
        assert registry["pagination"]["total"] == 2
        record = runtime.load_supply_calculation_registry_record(custom.calculation_id)
        assert record is not None
        assert record["calculation_type"] == "fbs_fulfillment_order"
        assert record["evidence"]["contract_name"] == (
            "wb-core.supply-calculation-evidence.fbs-fulfillment-order"
        )
        assert record["evidence"]["wb_stock_used"] is False
        assert record["evidence"]["target_facility"]["facility_id"] == MOSCOW_ID
        demand = record["evidence"]["demand_basis"]
        assert demand["sales_window"]["mode"] == "custom_period"
        assert demand["sales_window"]["actual_date_from"] == "2026-04-10"
        assert demand["outside_window_samples_used"] is False
        export_bytes, export_name, _ = runtime.load_supply_calculation_registry_export(
            custom.calculation_id
        )
        export_rows = read_first_sheet_rows(export_bytes)
        assert export_name.endswith(".xlsx")
        assert "Режим истории" in export_rows[0]
        assert "Последние N дней" in export_rows[0]
        assert "Включённые даты" in export_rows[0]
        assert "Исключённые даты" in export_rows[0]
        assert "Итоговый demand basis, шт/день" in export_rows[0]
        assert "Целевой фулфилмент" in export_rows[0]
        assert any("custom_period" in [str(cell) for cell in row] for row in export_rows)
        assert any("false" in [str(cell).lower() for cell in row] for row in export_rows)

    print("fbs_fulfillment_order_supply_smoke: ok")
    return 0


def _expect_error(
    block: FbsFulfillmentOrderBlock,
    payload: dict[str, object],
    expected: str,
) -> None:
    try:
        block.calculate(payload)
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def _seed_facilities(
    runtime: RegistryUploadDbBackedRuntime,
    active_nm_ids: list[int],
) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(9,1,1,'fbs-smoke',?,'{{}}')",
            (NOW_TEXT,),
        )
        for facility_id, code, name, city in (
            (MOSCOW_ID, "FF-MOSCOW", "FF Москва", "Москва"),
            (ORENBURG_ID, "FF-ORENBURG", "FF Оренбург", "Оренбург"),
        ):
            conn.execute(
                f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (facility_id, code, name, 1, "Asia/Yekaterinburg", NOW_TEXT, NOW_TEXT),
            )
            conn.execute(
                f"INSERT INTO {FACILITY_PROFILES_TABLE}(facility_id,city,future_fields_json,created_at,updated_at) VALUES(?,?,'{{}}',?,?)",
                (facility_id, city, NOW_TEXT, NOW_TEXT),
            )
        for index, nm_id in enumerate(active_nm_ids):
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                       source_watermark,updated_at
                   ) VALUES(?,'FBS',?,9,?,'0',NULL,'fbs-smoke',?)""",
                (MOSCOW_ID, nm_id, 100 if index == 0 else 200, NOW_TEXT),
            )
        conn.execute(
            f"""INSERT INTO {MANIFESTS_TABLE}(
                   cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,feature_epoch,
                   aggregate_revision,aggregate_digest,detail_digest,observation_watermark_sequence,
                   observation_watermark_digest,mapping_digest,fbw_origins_digest,
                   control_evidence_digest,non_target_digest,opening_document_id,
                   source_snapshot_digest,created_at,manifest_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "cutover-fbs-smoke", "sha256:manifest", "a" * 40, NOW_TEXT,
                "2026-04-18", 9, "fbs-smoke", "sha256:aggregate", "sha256:detail",
                1, "sha256:watermark", "sha256:mapping", "sha256:fbw",
                "sha256:control", "sha256:non-target", "opening-fbs-smoke",
                "sha256:source", NOW_TEXT, "{}",
            ),
        )
        conn.execute(
            f"""INSERT INTO {CURRENT_TABLE}(
                   cutover_id,order_id,state,episode_sequence,source_revision,status_digest,
                   supplier_status,wb_status,facility_id,pool,nm_id,quantity,frozen_wac_rub,
                   debit_event_id,updated_at
               ) VALUES('cutover-fbs-smoke',7001,'reserved',1,'sha256:order','sha256:status',
                        'confirm','waiting',?,'FBS',?,30,'0','',?)""",
            (MOSCOW_ID, active_nm_ids[0], NOW_TEXT),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,'aggregate-only-fbs',?)",
            (NOW_TEXT,),
        )
        aggregate_rows = [
            {
                "nmId": nm_id,
                "warehouseId": -999999,
                "warehouseName": "Склад WB",
                "regionName": "Склад WB",
                "quantity": 100_000 + index,
            }
            for index, nm_id in enumerate(active_nm_ids)
        ]
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                   snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
                   pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,
                   raw_rows_json,items_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "aggregate-only-fbs",
                "aggregate-only-fbs",
                NOW_TEXT,
                "2026-04-18",
                json.dumps(active_nm_ids),
                1,
                1,
                "[0]",
                len(aggregate_rows),
                "sha256:aggregate-only-fbs",
                json.dumps(aggregate_rows, ensure_ascii=False),
                json.dumps(
                    [
                        {"nm_id": row["nmId"], "quantity": row["quantity"]}
                        for row in aggregate_rows
                    ]
                ),
                NOW_TEXT,
            ),
        )
        conn.commit()


def _seed_sales_history(
    runtime: RegistryUploadDbBackedRuntime,
    active_nm_ids: list[int],
) -> None:
    items: list[SalesFunnelHistoryItem] = []
    for day in range(1, 18):
        snapshot_date = f"2026-04-{day:02d}"
        for index, nm_id in enumerate(active_nm_ids):
            value = 20.0 + index
            if day in {10, 12}:
                value = 10.0 + index
            elif day == 11:
                value = 1.0
            elif day in {9, 13}:
                value = 1000.0
            items.append(
                SalesFunnelHistoryItem(
                    date=snapshot_date,
                    nm_id=nm_id,
                    metric="orderCount",
                    value=value,
                )
            )
    persist_sales_history_result_exact_dates(
        runtime=runtime,
        payload=SalesFunnelHistorySuccess(
            kind="success",
            date_from="2026-04-01",
            date_to="2026-04-17",
            count=len(items),
            items=items,
        ),
        captured_at=NOW_TEXT,
    )


def _assert_target_facility_validation(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    runtime.save_supplier_shipment_upload(
        upload_id="target-validation-upload",
        created_at=NOW_TEXT,
        source_filename="target-validation.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source_file_sha256="sha256:target-validation",
        source_file_path="",
        parser_version="smoke",
        parsed_payload={
            "shipment_date": "2026-04-20",
            "metadata": {},
            "lines": [],
            "warnings": [],
            "errors": [],
        },
    )
    block = SupplierShipmentsBlock(
        runtime=runtime,
        timestamp_factory=lambda: NOW_TEXT,
    )
    options = block.list_shipments()["target_facility_options"]
    assert {item["facility_id"] for item in options} == {MOSCOW_ID, ORENBURG_ID}
    for target, expected in (
        ("", "обязательно выберите целевой фулфилмент"),
        ("inactive-or-missing", "не существует или не active"),
    ):
        try:
            block.create_shipment(
                {
                    "upload_id": "target-validation-upload",
                    "shipment_date": "2026-04-20",
                    "target_facility_id": target,
                }
            )
        except ValueError as exc:
            assert expected in str(exc), str(exc)
        else:
            raise AssertionError("new supplier order target validation must fail closed")


def _seed_shipments(
    runtime: RegistryUploadDbBackedRuntime,
    active_nm_ids: list[int],
) -> None:
    for shipment_id, target_id, quantity, status in (
        ("legacy-null-target", None, 20, ORDER_STATUS_PRODUCTION),
        ("explicit-moscow", MOSCOW_ID, 15, ORDER_STATUS_IN_TRANSIT),
        ("explicit-orenburg", ORENBURG_ID, 500, ORDER_STATUS_IN_TRANSIT),
        ("accepted-moscow", MOSCOW_ID, 400, ORDER_STATUS_ACCEPTED_FF),
    ):
        runtime.save_supplier_shipment(
            header={
                "shipment_id": shipment_id,
                "created_at": NOW_TEXT,
                "updated_at": NOW_TEXT,
                "shipment_date": "2026-04-20",
                "actual_shipment_date": (
                    "2026-04-20"
                    if status in {ORDER_STATUS_IN_TRANSIT, ORDER_STATUS_ACCEPTED_FF}
                    else ""
                ),
                "actual_ff_acceptance_date": (
                    "2026-04-25" if status == ORDER_STATUS_ACCEPTED_FF else ""
                ),
                "target_facility_id": target_id,
                "target_facility_name": "",
                "order_status": status,
                "invoice_no": shipment_id,
                "invoice_date": "2026-04-17",
                "currency": "RMB",
                "product_qty_total": quantity,
                "product_amount_total": quantity,
                "extras_amount_total": 0,
                "invoice_amount_total": quantity,
                "declared_invoice_total": quantity,
                "match_status": "all_matched",
                "source_filename": shipment_id + ".xlsx",
                "warnings": [],
                "errors": [],
            },
            lines=[
                {
                    "line_id": shipment_id + "-line",
                    "line_type": "product",
                    "sort_order": 1,
                    "source_no": "1",
                    "model_raw": "SKU",
                    "internal_nm_id": active_nm_ids[0],
                    "internal_name": "SKU",
                    "qty": quantity,
                    "unit_price": 1,
                    "amount": quantity,
                    "currency": "RMB",
                    "comment": "",
                    "match_status": "matched",
                    "manual_override": False,
                    "raw": {},
                }
            ],
        )


if __name__ == "__main__":
    raise SystemExit(main())
