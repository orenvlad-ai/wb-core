"""Targeted smoke-check for guarded WB checkpoint reconciliation and reversal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_stock_ledger import (  # noqa: E402
    FF_STOCK_OPERATION_AUTO_WRITEOFF,
    FF_STOCK_OPERATION_CORRECTION_RECEIPT,
    FF_STOCK_OPERATION_MANUAL_RECEIPT,
    FF_STOCK_SOURCE_MANUAL_EXCEL,
    FF_STOCK_SOURCE_TARGETED_RECONCILIATION,
    FF_STOCK_SOURCE_WB_SUPPLY,
    FfStockLedgerBlock,
    TargetedWbSupplyReconciliationError,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402


SUPPLY_ID = "40561872"
CACHE_KEY = f"supply:{SUPPLY_ID}"
SOURCE_KEY = f"wb_supply_debit:{CACHE_KEY}"
PREORDER_ID = "52530963"
NM_IDS = [9100000 + index for index in range(1, 14)]
DEBITS = [2400.0] * 12 + [2700.0]
REMAINDERS = [500.0] * 12 + [750.0]
OPENING_BALANCES = [debit + remainder for debit, remainder in zip(DEBITS, REMAINDERS)]
ACTIVATED_AT = "2026-07-01T00:00:00Z"
CHECKPOINT_AT = "2026-07-05T00:00:00Z"
APPLIED_AT = "2026-07-12T09:00:00Z"


def main() -> None:
    _happy_path_and_reversal()
    _blocked_statuses()
    _doprinato_blocked()
    _goods_missing_blocked()
    _insufficient_balance_blocked()
    _unexpected_total_blocked()
    _inactive_nomenclature_blocked()
    _stale_plan_variants_blocked()
    print("ff_stock_targeted_reconciliation_smoke: ok")


def _happy_path_and_reversal() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-happy-") as tmp:
        runtime, block = _setup(Path(tmp), status_id=2)
        before_checkpoint = runtime.load_ff_stock_wb_auto_writeoff_checkpoint()
        _save_target_supply(runtime, status_id=3)
        record = runtime.load_wb_supply_record(SUPPLY_ID)
        ordinary = block.record_wb_supply_debit(record or {})
        _assert(
            ordinary and ordinary.get("skip_reason") == "wb_supply_before_auto_writeoff_checkpoint",
            f"ordinary sync path must keep checkpoint guard, got {ordinary}",
        )
        operation_count_before = runtime.count_ff_stock_operations()
        balances_before = runtime.list_ff_stock_balances()
        plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        plan_repeat = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        _assert(plan["status"] == "dry_run" and plan["apply_allowed"], f"target plan must be applicable: {plan}")
        _assert(plan["fingerprint"] == plan_repeat["fingerprint"], "unchanged dry-run fingerprint must be stable")
        _assert(plan["supply"]["cache_key"] == CACHE_KEY and plan["supply"]["source_key"] == SOURCE_KEY, "canonical identity changed")
        _assert(plan["supply"]["preorder_id"] == PREORDER_ID and plan["supply"]["sku_count"] == 13, "supply identity/SKU count changed")
        _assert(plan["checkpoint"]["ordinary_path_reason"] == "wb_supply_before_auto_writeoff_checkpoint", "checkpoint reason missing")
        _assert(plan["totals"] == {"before": 38250.0, "debit": 31500.0, "after": 6750.0}, f"target totals changed: {plan['totals']}")
        _assert(len(plan["skus"]) == 13 and all(item["projected_balance"] >= 0 for item in plan["skus"]), "per-SKU projection invalid")
        _assert(runtime.count_ff_stock_operations() == operation_count_before, "dry-run must not create an operation")
        _assert(runtime.list_ff_stock_balances() == balances_before, "dry-run must not change balances")
        _assert(runtime.load_ff_stock_wb_auto_writeoff_checkpoint() == before_checkpoint, "dry-run must not change checkpoint")

        no_flag = _expect_error(
            lambda: block.apply_targeted_wb_supply_reconciliation(
                SUPPLY_ID,
                apply=False,
                confirmation_fingerprint=plan["fingerprint"],
                created_by="smoke",
            )
        )
        _assert(no_flag.code == "explicit_apply_required", "apply without explicit flag must fail")
        applied = block.apply_targeted_wb_supply_reconciliation(
            SUPPLY_ID,
            apply=True,
            confirmation_fingerprint=plan["fingerprint"],
            created_by="smoke",
        )
        operation = runtime.load_ff_stock_operation(applied["operation"]["operation_id"])
        _assert(applied["status"] == "applied" and not applied["idempotent"], f"target apply failed: {applied}")
        _assert(operation and operation["operation_type"] == FF_STOCK_OPERATION_AUTO_WRITEOFF, "operation type must be auto_writeoff")
        _assert(operation["source_type"] == FF_STOCK_SOURCE_WB_SUPPLY and operation["source_key"] == SOURCE_KEY, "operation source link changed")
        _assert(operation["source_object_id"] == SUPPLY_ID and SUPPLY_ID in operation["source_object_label"], "operation supply label changed")
        _assert(operation["diagnostics"]["reason"] == "targeted_checkpoint_reconciliation", "targeted audit reason missing")
        _assert(operation["diagnostics"]["cache_key"] == CACHE_KEY, "targeted diagnostics cache key missing")
        _assert(len(operation["lines"]) == 13 and operation["total_quantity_abs"] == 31500.0, "ledger lines/quantity changed")
        _assert(applied["post_run_reconciliation"]["ledger_total_after"] == 6750.0, "post-run total must reconcile")
        _assert(not applied["post_run_reconciliation"]["negative_affected_skus"], "apply must not create negative balances")
        _assert(runtime.load_ff_stock_wb_auto_writeoff_checkpoint() == before_checkpoint, "apply must not change checkpoint")

        repeat = block.apply_targeted_wb_supply_reconciliation(
            SUPPLY_ID,
            apply=True,
            confirmation_fingerprint=plan["fingerprint"],
            created_by="smoke",
        )
        _assert(repeat["status"] == "already_applied" and repeat["idempotent"], "repeated apply must be idempotent")
        ordinary_after = block.record_wb_supply_debit(runtime.load_wb_supply_record(SUPPLY_ID) or {})
        _assert(ordinary_after and ordinary_after.get("idempotent"), "ordinary next sync must see canonical source key")
        _assert(runtime.count_ff_stock_operations() == operation_count_before + 1, "apply/sync repeat must create exactly one debit")

        reversal_plan = block.plan_targeted_wb_supply_reversal(SUPPLY_ID)
        reversed_result = block.apply_targeted_wb_supply_reversal(
            SUPPLY_ID,
            apply=True,
            confirmation_fingerprint=reversal_plan["fingerprint"],
            created_by="smoke",
        )
        reversal = runtime.load_ff_stock_operation(reversed_result["operation"]["operation_id"])
        _assert(reversal and reversal["operation_type"] == FF_STOCK_OPERATION_CORRECTION_RECEIPT, "reversal must be compensating receipt")
        _assert(reversal["source_type"] == FF_STOCK_SOURCE_TARGETED_RECONCILIATION, "reversal source type changed")
        _assert(reversal["diagnostics"]["compensates_operation_id"] == operation["operation_id"], "reversal audit link missing")
        _assert(runtime.load_ff_stock_operation(operation["operation_id"]) is not None, "reversal must preserve original history")
        _assert(reversed_result["post_run_reconciliation"]["ledger_total_after"] == 38250.0, "reversal must restore ledger total")
        reversal_repeat = block.apply_targeted_wb_supply_reversal(
            SUPPLY_ID,
            apply=True,
            confirmation_fingerprint=reversal_plan["fingerprint"],
            created_by="smoke",
        )
        _assert(reversal_repeat["idempotent"], "repeated reversal must be idempotent")


def _blocked_statuses() -> None:
    for status_id in (1, 2):
        with TemporaryDirectory(prefix=f"ff-stock-targeted-status-{status_id}-") as tmp:
            _runtime, block = _setup(Path(tmp), status_id=status_id)
            plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
            _assert(not plan["apply_allowed"], f"status {status_id} must block targeted apply")
            _assert(_has_blocker(plan, "wb_supply_status_not_debit_eligible"), f"status {status_id} blocker missing")


def _doprinato_blocked() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-doprinato-") as tmp:
        _runtime, block = _setup(Path(tmp), status_id=3, virtual_type_id=5, type_label="Допринято")
        plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        _assert(not plan["apply_allowed"], "Допринято must block targeted apply")
        _assert(_has_blocker(plan, "wb_supply_doprinato_virtual_type"), "Допринято virtual blocker missing")
        _assert(_has_blocker(plan, "wb_supply_doprinato_type_label"), "Допринято label blocker missing")


def _goods_missing_blocked() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-no-goods-") as tmp:
        _runtime, block = _setup(Path(tmp), status_id=3, include_goods=False)
        plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        _assert(not plan["apply_allowed"] and _has_blocker(plan, "wb_supply_goods_missing"), "missing goods must fail closed")


def _insufficient_balance_blocked() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-shortage-") as tmp:
        runtime, block = _setup(Path(tmp), status_id=3, first_balance=DEBITS[0] - 1)
        before_count = runtime.count_ff_stock_operations()
        plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        blocker = next(item for item in plan["blockers"] if item["code"] == "wb_supply_would_make_negative_balance")
        shortage = blocker["skus"][0]
        _assert(
            shortage == {
                "nm_id": NM_IDS[0],
                "nmID": NM_IDS[0],
                "current_balance": DEBITS[0] - 1,
                "required_debit": DEBITS[0],
                "projected_balance": -1.0,
                "expected_balance": -1.0,
            },
            f"shortage details changed: {shortage}",
        )
        _assert(not plan["apply_allowed"] and runtime.count_ff_stock_operations() == before_count, "shortage must block whole operation")


def _unexpected_total_blocked() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-total-") as tmp:
        _runtime, block = _setup(Path(tmp), status_id=3, first_balance=OPENING_BALANCES[0] + 1)
        plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        _assert(not plan["apply_allowed"], "unexpected global FF total must block targeted apply")
        _assert(_has_blocker(plan, "target_ff_stock_total_before_changed"), "before-total blocker missing")
        _assert(_has_blocker(plan, "target_ff_stock_total_after_changed"), "after-total blocker missing")


def _inactive_nomenclature_blocked() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-nomenclature-") as tmp:
        runtime, block = _setup(Path(tmp), status_id=3)
        item = runtime.load_nomenclature_item(f"nom_{NM_IDS[0]}") or {}
        item["is_active"] = False
        item["updated_at"] = "2026-07-12T08:00:00Z"
        runtime.save_nomenclature_item(item)
        plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
        _assert(not plan["apply_allowed"], "inactive nomenclature must block targeted apply")
        _assert(_has_blocker(plan, "wb_supply_goods_nm_id_not_in_active_nomenclature"), "nomenclature blocker missing")


def _stale_plan_variants_blocked() -> None:
    for variant in ("goods", "status", "balances"):
        with TemporaryDirectory(prefix=f"ff-stock-targeted-stale-{variant}-") as tmp:
            runtime, block = _setup(Path(tmp), status_id=3)
            plan = block.plan_targeted_wb_supply_reconciliation(SUPPLY_ID)
            if variant == "goods":
                changed = list(DEBITS)
                changed[0] += 1
                _save_target_supply(runtime, status_id=3, debit_quantities=changed)
            elif variant == "status":
                _save_target_supply(runtime, status_id=4)
            else:
                runtime.create_ff_stock_operation(
                    operation_id="ffso_stale_balance",
                    operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
                    source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
                    source_key="manual_excel:stale-balance",
                    source_object_id="stale-balance",
                    source_object_label="stale balance",
                    created_at="2026-07-11T00:00:00Z",
                    created_by="smoke",
                    lines=[{"nm_id": NM_IDS[0], "quantity_delta": 1.0}],
                )
            error = _expect_error(
                lambda: block.apply_targeted_wb_supply_reconciliation(
                    SUPPLY_ID,
                    apply=True,
                    confirmation_fingerprint=plan["fingerprint"],
                    created_by="smoke",
                )
            )
            _assert(error.code == "stale_or_invalid_fingerprint", f"{variant} change must stale fingerprint, got {error.code}")
            _assert(runtime.load_ff_stock_operation_by_source_key(SOURCE_KEY) is None, f"stale {variant} plan must not apply")


def _setup(
    root: Path,
    *,
    status_id: int,
    virtual_type_id: int | None = None,
    type_label: str = "Короб",
    include_goods: bool = True,
    first_balance: float | None = None,
) -> tuple[RegistryUploadDbBackedRuntime, FfStockLedgerBlock]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root / "runtime")
    _seed_nomenclature(runtime)
    balances = list(OPENING_BALANCES)
    if first_balance is not None:
        balances[0] = float(first_balance)
    runtime.create_ff_stock_operation(
        operation_id="ffso_opening",
        operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
        source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
        source_key="manual_excel:targeted-opening",
        source_object_id="targeted-opening",
        source_object_label="Targeted reconciliation opening balance",
        created_at=ACTIVATED_AT,
        created_by="smoke",
        lines=[{"nm_id": nm_id, "quantity_delta": balance} for nm_id, balance in zip(NM_IDS, balances)],
    )
    _save_target_supply(
        runtime,
        status_id=status_id,
        virtual_type_id=virtual_type_id,
        type_label=type_label,
        debit_quantities=DEBITS if include_goods else None,
    )
    runtime.save_ff_stock_wb_auto_writeoff_checkpoint(
        checkpoint_id="ffswc_targeted_smoke",
        created_at=CHECKPOINT_AT,
        created_by="smoke",
        reason="targeted smoke baseline",
        baseline_cache_keys=[CACHE_KEY],
        baseline_source_keys=[SOURCE_KEY],
        baseline_supply_ids=[SUPPLY_ID],
    )
    return runtime, FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: APPLIED_AT)


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_sku_group(
        {
            "group_key": "targeted",
            "label": "Targeted",
            "is_active": True,
            "is_system": False,
            "created_at": ACTIVATED_AT,
            "updated_at": ACTIVATED_AT,
        }
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"nom_{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"TARGET-{index:02d}",
                "nm_id": nm_id,
                "barcode": f"460{nm_id}",
                "barcodes": [f"460{nm_id}"],
                "nomenclature_name": f"Target SKU {index}",
                "product_type": "targeted",
                "match_key": f"target-{index}",
                "comment": "targeted reconciliation smoke",
                "created_at": ACTIVATED_AT,
                "updated_at": ACTIVATED_AT,
            }
            for index, nm_id in enumerate(NM_IDS, start=1)
        ]
    )


def _save_target_supply(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    status_id: int,
    virtual_type_id: int | None = None,
    type_label: str = "Короб",
    debit_quantities: list[float] | None = None,
) -> None:
    raw_goods = (
        [{"nmID": nm_id, "barcode": f"460{nm_id}", "quantity": quantity} for nm_id, quantity in zip(NM_IDS, debit_quantities)]
        if debit_quantities is not None
        else None
    )
    raw_goods_hash = (
        hashlib.sha256(json.dumps(raw_goods, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if raw_goods is not None
        else ""
    )
    row = {
        "supply_id": SUPPLY_ID,
        "cache_key": CACHE_KEY,
        "wb_supply_id": SUPPLY_ID,
        "preorder_id": PREORDER_ID,
        "visible_number": SUPPLY_ID,
        "number_label": SUPPLY_ID,
        "status_id": status_id,
        "status_label": {1: "Не запланировано", 2: "Запланировано", 3: "Отгрузка разрешена", 4: "Идёт приёмка"}.get(status_id, str(status_id)),
        "virtual_type_id": virtual_type_id,
        "type_label": type_label,
        "source_created_at": "2026-07-02T15:38:24+03:00",
        "supply_date": "2026-07-14T00:00:00+03:00",
        "quantity_for_size_filter": sum(debit_quantities or []),
        "raw_list": {"supplyID": int(SUPPLY_ID), "preorderID": int(PREORDER_ID), "statusID": status_id},
        "raw_detail": {"supplyID": int(SUPPLY_ID), "statusID": status_id},
        "raw_package": [],
        "raw_goods_hash": raw_goods_hash,
    }
    if raw_goods is not None:
        row["raw_goods"] = raw_goods
    runtime.save_wb_supply_rows(rows=[row], warehouses=[], synced_at="2026-07-10T00:00:00Z")


def _expect_error(callback: object) -> TargetedWbSupplyReconciliationError:
    try:
        callback()  # type: ignore[operator]
    except TargetedWbSupplyReconciliationError as exc:
        return exc
    raise AssertionError("expected TargetedWbSupplyReconciliationError")


def _has_blocker(plan: dict[str, object], code: str) -> bool:
    return any(str(item.get("code") or "") == code for item in plan.get("blockers") or [])  # type: ignore[union-attr]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
