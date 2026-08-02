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
from packages.application.warehouse_functional import (  # noqa: E402
    ensure_warehouse_functional_schema,
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
        _seed_ff_functional_costs(runtime, [nm_activation, nm_future])
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
        _seed_ff_functional_costs(runtime, [nm_activation, nm_future, nm_physical])
        _save_supply(runtime, "wait-cost", 3, [(nm_physical, 5)], revision="1")
        waiting_cost = block.record_wb_supply_debit(
            next(item for item in runtime.list_wb_supplies_cache_records() if item["supply_id"] == "wait-cost")
        )
        _assert(
            waiting_cost and waiting_cost.get("operation_id"),
            waiting_cost,
        )
        _assert(
            _balance(runtime, nm_physical) == 5.0,
            "missing cost must not block a physically proven movement",
        )
        _assert(
            runtime.list_ff_stock_reservations(supply_id="wait-cost") == [],
            "cost-only reservation must not be created",
        )
        _seed_valid_cost(runtime, "wait-cost", nm_physical)
        completed = block.record_wb_supply_debit(
            next(item for item in runtime.list_wb_supplies_cache_records() if item["supply_id"] == "wait-cost")
        )
        _assert(completed and completed.get("idempotent"), completed)
        _assert(
            _balance(runtime, nm_physical) == 5.0,
            "late cost replay must not repeat the physical debit",
        )
        _assert(runtime.list_ff_stock_reservations(supply_id="wait-cost") == [], "fulfilled reserve remained active")

        _save_supply(
            runtime,
            "atomic-composition",
            3,
            [(nm_activation, 1), (nm_future, 1)],
            revision="1",
        )
        _seed_ff_functional_costs(runtime, [nm_activation, nm_future, nm_physical])
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
    _test_four_supply_43000_atomic_fulfillment()
    _test_intervening_positive_movement_invalidates_ff_cost()
    _test_supply_lifecycle_returns()
    print("ff_stock_reservation_smoke: OK")


def _test_four_supply_43000_atomic_fulfillment() -> None:
    with TemporaryDirectory(prefix="ff-reservation-43000-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.ingest_bundle(
            json.loads(FIXTURE.read_text(encoding="utf-8")),
            activated_at=ACTIVATED_AT,
        )
        nm_ids = [
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled and item.nm_id is not None
        ][:2]
        if len(nm_ids) < 2:
            raise AssertionError("43,000 reservation smoke requires two active SKU")
        _seed_nomenclature(runtime, nm_ids)
        activation_nm, target_nm = nm_ids
        runtime.create_ff_stock_operation(
            operation_id="ffso-43000-activation",
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key="reservation-43000:activation",
            source_object_id="reservation-43000",
            source_object_label="43,000 reservation activation",
            created_at=ACTIVATED_AT,
            created_by="smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": activation_nm, "quantity_delta": 1}],
        )
        block = FfStockLedgerBlock(
            runtime=runtime,
            timestamp_factory=lambda: CHECKPOINT_AT,
        )
        block.ensure_wb_supply_auto_writeoff_checkpoint(
            [], reason="reservation_43000_smoke"
        )
        _append_receipt(
            runtime,
            target_nm,
            74500,
            source_key="reservation-43000:physical",
        )
        _seed_ff_functional_costs(runtime, [activation_nm, target_nm])
        targets = {
            "41058085": 5750,
            "41058204": 6250,
            "41058408": 14000,
            "41058611": 17000,
        }
        for supply_id, quantity in targets.items():
            _save_supply(
                runtime,
                supply_id,
                3,
                [(target_nm, quantity)],
                revision="1",
            )
        _save_supply(
            runtime,
            "non-target-waiting",
            3,
            [(activation_nm, 10)],
            revision="1",
        )
        waiting = block.record_wb_supply_debits(
            runtime.list_wb_supplies_cache_records()
        )
        target_reserved = sum(
            float(item["quantity"])
            for supply_id in targets
            for item in runtime.list_ff_stock_reservations(supply_id=supply_id)
        )
        _assert(target_reserved == 0.0, waiting)
        _assert(
            _balance(runtime, target_nm) == 31500.0,
            "four cost-pending supplies must debit exactly 43,000 physical units",
        )
        _assert(waiting["created_count"] == 4, waiting)
        for supply_id in targets:
            _seed_valid_cost(runtime, supply_id, target_nm)
        fulfilled = block.record_wb_supply_debits(
            runtime.list_wb_supplies_cache_records()
        )
        _assert(fulfilled["created_count"] == 0, fulfilled)
        _assert(
            len(set(waiting["created_operation_ids"])) == 4
            and all(
                str(operation_id).startswith("ffso_")
                and ":" not in str(operation_id)
                for operation_id in waiting["created_operation_ids"]
            ),
            "physical debit identities must be deterministic source-derived IDs",
        )
        _assert(
            sum(
                len(runtime.list_ff_stock_reservations(supply_id=supply_id))
                for supply_id in targets
            )
            == 0,
            "four exact target reservations were not fulfilled",
        )
        _assert(
            len(runtime.list_ff_stock_reservations(supply_id="non-target-waiting"))
            == 1,
            "unrelated waiting reservation changed",
        )
        _assert(
            _balance(runtime, target_nm) == 31500.0,
            "four target debits must remove exactly 43,000 from physical FF",
        )
        for supply_id in targets:
            _assert(
                runtime.load_ff_stock_operation_by_source_key(
                    "wb_supply_debit:" + supply_id
                )
                is not None,
                f"exact physical debit missing for {supply_id}",
            )
        repeated = block.record_wb_supply_debits(
            runtime.list_wb_supplies_cache_records()
        )
        _assert(repeated["created_count"] == 0, repeated)
        _assert(
            _balance(runtime, target_nm) == 31500.0,
            "repeated reconciliation double-debited one of four supplies",
        )


def _test_intervening_positive_movement_invalidates_ff_cost() -> None:
    with TemporaryDirectory(prefix="ff-cost-positive-staleness-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.ingest_bundle(
            json.loads(FIXTURE.read_text(encoding="utf-8")),
            activated_at=ACTIVATED_AT,
        )
        nm_id = next(
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled and item.nm_id is not None
        )
        _seed_nomenclature(runtime, [nm_id])
        runtime.create_ff_stock_operation(
            operation_id="ffso-cost-stale-opening",
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key="cost-stale:opening",
            source_object_id="cost-stale",
            source_object_label="cost stale opening",
            created_at=ACTIVATED_AT,
            created_by="smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": nm_id, "quantity_delta": 10}],
        )
        block = FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: CHECKPOINT_AT)
        block.ensure_wb_supply_auto_writeoff_checkpoint([], reason="cost_stale_smoke")
        block = FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: SOURCE_AT)
        _seed_ff_functional_costs(runtime, [nm_id])
        for operation_id, delta in (("receipt", 5), ("writeoff", -5)):
            runtime.create_ff_stock_operation(
                operation_id=f"ffso-cost-stale-{operation_id}",
                operation_type=(
                    FF_STOCK_OPERATION_MANUAL_RECEIPT
                    if delta > 0
                    else "manual_writeoff"
                ),
                source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
                source_key=f"cost-stale:{operation_id}",
                source_object_id="cost-stale",
                source_object_label="cost stale same-net movement",
                created_at=SOURCE_AT,
                created_by="smoke",
                warnings=[],
                diagnostics={},
                lines=[{"nm_id": nm_id, "quantity_delta": delta}],
            )
        _save_supply(runtime, "cost-stale-supply", 3, [(nm_id, 1)], revision="1")
        result = block.record_wb_supply_debit(
            next(
                item
                for item in runtime.list_wb_supplies_cache_records()
                if item["supply_id"] == "cost-stale-supply"
            )
        )
        _assert(
            result
            and result.get("skip_reason") == "wb_supply_ff_cost_snapshot_missing"
            and (result.get("cost_snapshot_blockers") or [])[0].get("reason")
            == "active_ff_cost_precedes_positive_movement",
            result,
        )
        _assert(_balance(runtime, nm_id) == 10.0, "stale cost allowed a physical debit")


def _test_supply_lifecycle_returns() -> None:
    with TemporaryDirectory(prefix="ff-supply-lifecycle-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.ingest_bundle(
            json.loads(FIXTURE.read_text(encoding="utf-8")),
            activated_at=ACTIVATED_AT,
        )
        nm_id = next(
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled and item.nm_id is not None
        )
        _seed_nomenclature(runtime, [nm_id])
        runtime.create_ff_stock_operation(
            operation_id="ffso-lifecycle-activation",
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key="lifecycle:activation",
            source_object_id="lifecycle",
            source_object_label="lifecycle activation",
            created_at=ACTIVATED_AT,
            created_by="smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": nm_id, "quantity_delta": 10}],
        )
        block = FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: CHECKPOINT_AT)
        block.ensure_wb_supply_auto_writeoff_checkpoint([], reason="lifecycle_smoke")
        _seed_ff_functional_costs(runtime, [nm_id])
        _save_supply(runtime, "lifecycle-partial", 3, [(nm_id, 10)], revision="1")
        record = next(
            item
            for item in runtime.list_wb_supplies_cache_records()
            if item["supply_id"] == "lifecycle-partial"
        )
        debited = block.record_wb_supply_debit(record)
        _assert(debited and debited.get("operation_id"), debited)
        _assert(_balance(runtime, nm_id) == 0.0, "lifecycle debit was not physical")
        lifecycle_record = {**record, "raw_goods": [{"nmID": nm_id, "quantity": 10, "acceptedQuantity": 4}]}
        historical_false_cancel = {
            "supply_id": "lifecycle-historical-accepted",
            "normalized": {
                "supply_id": "lifecycle-historical-accepted",
                "wb_supply_id": "lifecycle-historical-accepted",
                "status_id": 6,
                "is_cancelled": "false",
            },
            "raw_goods": [
                {"nmID": nm_id, "quantity": 10, "acceptedQuantity": 10}
            ],
        }
        first_seen = block.reconcile_wb_supply_lifecycle(
            records=[lifecycle_record, historical_false_cancel],
            active_authoritative_keys={"lifecycle-partial"},
            observation_id="complete-1",
            observed_at="2026-08-02T10:00:00Z",
        )
        _assert(first_seen["return_created_count"] == 0, first_seen)
        with sqlite3.connect(runtime.db_path) as conn:
            historical_lifecycle_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_wb_supply_lifecycle "
                    "WHERE supply_id='lifecycle-historical-accepted'"
                ).fetchone()[0]
            )
        _assert(
            historical_lifecycle_count == 0,
            "historical accepted supply with string false cancellation entered debounce",
        )
        first_gap = block.reconcile_wb_supply_lifecycle(
            records=[lifecycle_record],
            active_authoritative_keys=set(),
            observation_id="complete-2",
            observed_at="2026-08-02T11:00:00Z",
        )
        _assert(first_gap["return_created_count"] == 0, first_gap)
        same_gap = block.reconcile_wb_supply_lifecycle(
            records=[lifecycle_record],
            active_authoritative_keys=set(),
            observation_id="complete-2",
            observed_at="2026-08-02T11:01:00Z",
        )
        _assert(same_gap["return_created_count"] == 0, same_gap)
        returned = block.reconcile_wb_supply_lifecycle(
            records=[lifecycle_record],
            active_authoritative_keys=set(),
            observation_id="complete-3",
            observed_at="2026-08-02T12:00:00Z",
        )
        _assert(returned["return_created_count"] == 1, returned)
        _assert(_balance(runtime, nm_id) == 6.0, "only the unaccepted remainder must return")
        repeated = block.reconcile_wb_supply_lifecycle(
            records=[lifecycle_record],
            active_authoritative_keys=set(),
            observation_id="complete-4",
            observed_at="2026-08-02T13:00:00Z",
        )
        _assert(repeated["return_created_count"] == 0, repeated)
        original_return_operation_id = str(returned["return_operation_ids"][0])
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_ff_stock_wb_supply_lifecycle
                SET lifecycle_state='missing_confirmed',return_operation_id='',
                    last_observation_id='complete-recovery',
                    consecutive_missing_complete_snapshots=3
                WHERE supply_id='lifecycle-partial'
                """
            )
            conn.commit()
        recovered_existing_return = block.apply_confirmed_wb_supply_returns()
        _assert(
            original_return_operation_id
            in recovered_existing_return["return_operation_ids"],
            recovered_existing_return,
        )
        _assert(
            _balance(runtime, nm_id) == 6.0,
            "changed observation evidence created a second economic return",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            economic_return_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                    "WHERE source_type='wb_supply_return' AND source_object_id='lifecycle-partial'"
                ).fetchone()[0]
            )
        _assert(economic_return_count == 1, "one economic return per supply revision is allowed")
        reappeared = block.reconcile_wb_supply_lifecycle(
            records=[lifecycle_record],
            active_authoritative_keys={"lifecycle-partial"},
            observation_id="complete-5",
            observed_at="2026-08-02T14:00:00Z",
        )
        _assert(reappeared["return_created_count"] == 0, reappeared)
        reappearance_debit = block.record_wb_supply_debit(record)
        _assert(
            reappearance_debit and reappearance_debit.get("reappearance_redebit") is True,
            reappearance_debit,
        )
        _assert(
            _balance(runtime, nm_id) == 0.0,
            "reappearance must restore exact conservation after the prior return",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_ff_stock_wb_supply_lifecycle
                SET lifecycle_state='returned_reappeared',return_operation_id=?,
                    return_source_revision='crash-recovery-fixture'
                WHERE supply_id='lifecycle-partial'
                """,
                (original_return_operation_id,),
            )
            conn.commit()
        resumed_reappearance = block.record_wb_supply_debit(record)
        _assert(
            resumed_reappearance
            and resumed_reappearance.get("idempotent")
            and resumed_reappearance.get("reappearance_redebit") is True,
            resumed_reappearance,
        )
        _assert(
            _balance(runtime, nm_id) == 0.0,
            "crash recovery after committed reappearance redebit changed FF twice",
        )
        repeated_reappearance = block.record_wb_supply_debit(record)
        _assert(repeated_reappearance and repeated_reappearance.get("idempotent"), repeated_reappearance)
        _assert(_balance(runtime, nm_id) == 0.0, "reappearance was debited twice")
        with sqlite3.connect(runtime.db_path) as conn:
            reappearance_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                    "WHERE source_type='wb_supply_reappearance'"
                ).fetchone()[0]
            )
        _assert(reappearance_count == 1, "exactly one reappearance debit is allowed")


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


def _seed_ff_functional_costs(
    runtime: RegistryUploadDbBackedRuntime,
    nm_ids: list[int],
) -> None:
    version_id = "reservation-smoke-functional"
    balances = {
        int(item["nm_id"]): float(item["balance"])
        for item in runtime.list_ff_stock_balances()
    }
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,
                created_at,business_effective_date,published_at
            ) VALUES(?,?,'fixture','2026-07-20T10:00:00Z','good',?,?,?,?,'2026-07-20',?)
            """,
            (
                version_id,
                "warehouse_functional_cutover_v1",
                "sha256:reservation-functional",
                "sha256:reservation-functional-source",
                "{}",
                CHECKPOINT_AT,
                CHECKPOINT_AT,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,?,?)",
            (version_id, CHECKPOINT_AT),
        )
        for nm_id in nm_ids:
            quantity = balances.get(int(nm_id), 0.0)
            conn.execute(
                """
                INSERT OR REPLACE INTO sheet_vitrina_v1_warehouse_functional_balances(
                    version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                    cost_covered_quantity,quality,certified,wb_quantity,
                    wb_in_way_to_client,wb_in_way_from_client,provenance_json
                ) VALUES(?,'ff',?,?,?,?,?,'fixture',1,'0','0','0','{}')
                """,
                (
                    version_id,
                    int(nm_id),
                    str(quantity),
                    "100",
                    str(quantity * 100),
                    str(quantity),
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
