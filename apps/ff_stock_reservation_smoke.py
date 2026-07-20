"""Regression checks for append-only FF reservations and atomic fulfillment."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_stock_ledger import (  # noqa: E402
    FF_STOCK_OPERATION_MANUAL_RECEIPT,
    FF_STOCK_SOURCE_MANUAL_EXCEL,
    FfStockLedgerBlock,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


ACTIVATED_AT = "2026-07-20T09:00:00Z"
CHECKPOINT_AT = "2026-07-20T10:00:00Z"
SOURCE_AT = "2026-07-20T10:01:00Z"
FIXTURE = ROOT / "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json"


def main() -> None:
    with TemporaryDirectory(prefix="ff-reservation-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.ingest_bundle(json.loads(FIXTURE.read_text(encoding="utf-8")), activated_at=ACTIVATED_AT)
        nm_ids = [
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled and item.nm_id is not None
        ][:3]
        if len(nm_ids) < 3:
            raise AssertionError("reservation smoke requires three active SKU")
        _seed_nomenclature(runtime, nm_ids)
        nm_activation, nm_future, nm_physical = nm_ids
        runtime.create_ff_stock_operation(
            operation_id="ffso-reservation-activation",
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key="reservation-smoke:activation",
            source_object_id="reservation-smoke",
            source_object_label="reservation smoke activation",
            created_at=ACTIVATED_AT,
            created_by="smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": nm_activation, "quantity_delta": 1}],
        )
        block = FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: CHECKPOINT_AT)
        block.ensure_wb_supply_auto_writeoff_checkpoint([], reason="reservation_smoke")

        _save_supply(runtime, "reserve-a", 3, [(nm_future, 80)], revision="1")
        _save_supply(runtime, "reserve-b", 3, [(nm_future, 50)], revision="1")
        first = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(first["reservation_summary"]["reserved_quantity"] == 130.0, first)
        _assert(first["reservation_summary"]["unsecured_reservation_quantity"] == 130.0, first)
        _assert(_balance(runtime, nm_future) == 0.0, "reservation changed physical quantity")

        repeated = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(repeated["created_count"] == 0, repeated)
        _assert(repeated["reservation_summary"]["reserved_quantity"] == 130.0, repeated)

        _save_supply(runtime, "reserve-a", 3, [(nm_future, 60)], revision="2")
        adjusted = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(adjusted["reservation_summary"]["reserved_quantity"] == 110.0, adjusted)

        _save_supply(runtime, "reserve-b", 2, [(nm_future, 50)], revision="cancelled")
        cancelled = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(cancelled["reservation_summary"]["reserved_quantity"] == 60.0, cancelled)
        _assert(cancelled["reservation_release_count"] == 1, cancelled)

        _save_supply(runtime, "reserve-b", 3, [(nm_future, 50)], revision="reactivated")
        reactivated = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(reactivated["reservation_summary"]["reserved_quantity"] == 110.0, reactivated)
        _save_supply(runtime, "reserve-b", 2, [(nm_future, 50)], revision="cancelled-again")
        cancelled_again = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(cancelled_again["reservation_summary"]["reserved_quantity"] == 60.0, cancelled_again)
        _assert(cancelled_again["reservation_release_count"] == 1, cancelled_again)

        _append_receipt(runtime, nm_future, 60, source_key="reservation-smoke:future-receipt")
        _seed_valid_cost(runtime, "reserve-a", nm_future)
        fulfilled = block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        _assert(fulfilled["created_count"] == 1, fulfilled)
        _assert(fulfilled["reservation_summary"]["reserved_quantity"] == 0.0, fulfilled)
        _assert(_balance(runtime, nm_future) == 0.0, "fulfillment did not debit at receipt WAC boundary")
        _assert(
            runtime.load_ff_stock_operation_by_source_key("wb_supply_debit:reserve-a") is not None,
            "atomic physical debit is missing",
        )

        _append_receipt(runtime, nm_physical, 10, source_key="reservation-smoke:physical")
        _save_supply(runtime, "wait-cost", 3, [(nm_physical, 5)], revision="1")
        waiting_cost = block.record_wb_supply_debit(
            next(item for item in runtime.list_wb_supplies_cache_records() if item["supply_id"] == "wait-cost")
        )
        _assert(
            waiting_cost and waiting_cost.get("skip_reason")
            == "wb_supply_reserved_waiting_for_validated_downstream_costs",
            waiting_cost,
        )
        _assert(_balance(runtime, nm_physical) == 10.0, "missing cost must fail physical movement closed")
        _seed_valid_cost(runtime, "wait-cost", nm_physical)
        completed = block.record_wb_supply_debit(
            next(item for item in runtime.list_wb_supplies_cache_records() if item["supply_id"] == "wait-cost")
        )
        _assert(completed and completed.get("operation_id"), completed)
        _assert(_balance(runtime, nm_physical) == 5.0, "validated cost must allow the exact physical debit")
        _assert(runtime.list_ff_stock_reservations(supply_id="wait-cost") == [], "fulfilled reserve remained active")

        _save_supply(
            runtime,
            "atomic-composition",
            3,
            [(nm_activation, 1), (nm_future, 1)],
            revision="1",
        )
        _seed_valid_cost(runtime, "atomic-composition", nm_activation)
        _seed_valid_cost(runtime, "atomic-composition", nm_future)
        atomic = block.record_wb_supply_debit(
            next(
                item
                for item in runtime.list_wb_supplies_cache_records()
                if item["supply_id"] == "atomic-composition"
            )
        )
        _assert(atomic and atomic.get("skip_reason") == "wb_supply_reserved_waiting_for_goods", atomic)
        _assert(_balance(runtime, nm_activation) == 1.0, "one available SKU was partially debited")
        _save_supply(runtime, "atomic-composition", 2, [(nm_activation, 1), (nm_future, 1)], revision="cancelled")
        block.record_wb_supply_debits(runtime.list_wb_supplies_cache_records())
        runtime.create_ff_stock_operation(
            operation_id="ffso-negative-legacy-probe",
            operation_type="manual_writeoff",
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key="reservation-smoke:negative-legacy-probe",
            source_object_id="negative-legacy-probe",
            source_object_label="negative legacy probe",
            created_at=CHECKPOINT_AT,
            created_by="smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": nm_activation, "quantity_delta": -2}],
        )
        negative_row = next(
            item for item in block.current_balance_rows() if int(item["nm_id"]) == nm_activation
        )
        _assert(negative_row["current_stock_ff"] == -1.0, negative_row)
        _assert(negative_row["reserved_quantity"] == 0.0, negative_row)
        _assert(negative_row["unsecured_reservation_quantity"] == 0.0, negative_row)
        _assert(negative_row["reservation_status"] == "", negative_row)
        print("ff_stock_reservation_smoke: OK")


def _save_supply(
    runtime: RegistryUploadDbBackedRuntime,
    supply_id: str,
    status_id: int,
    goods: list[tuple[int, float]],
    *,
    revision: str,
) -> None:
    runtime.save_wb_supply_rows(
        rows=[
            {
                "supply_id": supply_id,
                "cache_key": supply_id,
                "wb_supply_id": supply_id,
                "preorder_id": "pre-" + supply_id,
                "number_label": supply_id,
                "status_id": status_id,
                "status_label": "Отгрузка разрешена" if status_id == 3 else "Создано",
                "source_created_at": SOURCE_AT,
                "updated_date": SOURCE_AT + revision,
                "supply_date": "2026-08-01",
                "raw_list": {"supplyID": supply_id, "statusID": status_id, "revision": revision},
                "raw_detail": {"supplyID": supply_id, "revision": revision},
                "raw_goods": [
                    {"nmID": int(nm_id), "quantity": float(quantity)}
                    for nm_id, quantity in goods
                ],
                "raw_package": [],
            }
        ],
        warehouses=[],
        synced_at=SOURCE_AT,
    )


def _seed_nomenclature(
    runtime: RegistryUploadDbBackedRuntime,
    nm_ids: list[int],
) -> None:
    runtime.save_sku_group(
        {
            "group_key": "reservation-smoke",
            "label": "Reservation smoke",
            "is_active": True,
            "is_system": False,
            "created_at": ACTIVATED_AT,
            "updated_at": ACTIVATED_AT,
        }
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"reservation-nom-{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"RES-{index}",
                "nm_id": nm_id,
                "barcode": f"460{nm_id}",
                "barcodes": [f"460{nm_id}"],
                "nomenclature_name": f"Reservation SKU {index}",
                "product_type": "reservation-smoke",
                "match_key": f"reservation-{index}",
                "comment": "",
                "created_at": ACTIVATED_AT,
                "updated_at": ACTIVATED_AT,
            }
            for index, nm_id in enumerate(nm_ids, start=1)
        ]
    )


def _append_receipt(
    runtime: RegistryUploadDbBackedRuntime,
    nm_id: int,
    quantity: float,
    *,
    source_key: str,
) -> None:
    runtime.create_ff_stock_operation(
        operation_id="ffso-" + source_key.replace(":", "-"),
        operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
        source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
        source_key=source_key,
        source_object_id=source_key,
        source_object_label=source_key,
        created_at=CHECKPOINT_AT,
        created_by="smoke",
        warnings=[],
        diagnostics={},
        lines=[{"nm_id": int(nm_id), "quantity_delta": float(quantity)}],
    )


def _seed_valid_cost(
    runtime: RegistryUploadDbBackedRuntime,
    supply_id: str,
    nm_id: int,
) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sheet_vitrina_v1_wb_supply_cost_layers(
                wb_supply_cost_layer_id,wb_supply_id,cache_key,nm_id,
                accepted_qty,qty_denominator,supply_date,accepted_date,
                sku_ff_unit_cost_rub,transit_cost_status,transit_amount_total,
                transit_per_unit_rub,ff_services_amount_total,ff_services_per_unit_rub,
                ff_storage_amount_total,ff_storage_per_unit_rub,
                pre_acceptance_unit_cost_rub,wb_acceptance_amount_total,
                wb_acceptance_per_accepted_unit_rub,our_wb_unit_cost_rub,
                source_status,component_status_json,missing_reason,calculated_at,
                inputs_hash,version,is_current
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (
                f"cost-{supply_id}-{nm_id}", supply_id, supply_id, int(nm_id),
                0, 1, "2026-07-20", None, 100, "direct_zero_confirmed", 0,
                0, 0, 0, 0, 0, 100, 0, 0, 100, "estimated", "{}", None,
                CHECKPOINT_AT, f"sha256:{supply_id}:{nm_id}", 1,
            ),
        )
        conn.commit()


def _balance(runtime: RegistryUploadDbBackedRuntime, nm_id: int) -> float:
    rows = {int(item["nm_id"]): float(item["balance"]) for item in runtime.list_ff_stock_balances()}
    return rows.get(int(nm_id), 0.0)


def _assert(condition: object, detail: object) -> None:
    if not condition:
        raise AssertionError(detail)


if __name__ == "__main__":
    main()
