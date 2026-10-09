"""Native bounded recovery rotation shared by admitted warehouse sync paths."""
import sqlite3
from typing import Any, Mapping

from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry


def run_bounded_recovery_retention(runtime: Any) -> dict[str, Any]:
    # Callers already hold their job/admission ownership. Serialize exact
    # retention plans with native writers, preserving job -> writer lock order.
    with warehouse_functional_write_lock(runtime.runtime_dir):
        return _run_locked_retention(runtime)


def _run_locked_retention(
    runtime: Any,
) -> dict[str, Any]:
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    with sqlite3.connect(
        f"file:{runtime.db_path}?mode=ro",
        uri=True,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        active_row = conn.execute(
            "SELECT version_id FROM "
            "sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()
        if int(conn.total_changes) != 0:
            raise RuntimeError(
                "warehouse recovery active-version readback mutated SQLite"
            )
    precheckpoint_reconciliation = (
        registry.reconcile_failed_hourly_precheckpoint_locks(
            current_active_version_id=(
                str(active_row[0]) if active_row is not None else ""
            ),
        )
    )
    blocking = [
        operation
        for operation in registry.list_operations(limit=1000)
        if operation.get("tier") == "T2"
        and operation.get("lifecycle")
        in {"failed_recoverable", "quarantined"}
    ]
    if blocking:
        raise RuntimeError(
            "warehouse recovery contains unresolved protected T2 evidence; "
            "another domain checkpoint is blocked: "
            + ",".join(
                str(operation.get("operation_id") or "")
                for operation in blocking[:10]
            )
        )
    plan = registry.plan_retention()
    if not bool(plan.get("would_change")):
        return {
            **plan,
            "status": "no_change",
            "applied": False,
            "precheckpoint_reconciliation": precheckpoint_reconciliation,
            "next_checkpoint_budget": _next_checkpoint_budget(registry, plan),
        }
    result = registry.apply_retention(
        plan_fingerprint=str(plan["fingerprint"]),
    )
    if str(result.get("status") or "") == "partial_failure":
        capacity = registry.capacity_status()
        raise RuntimeError(
            "warehouse recovery retention could not prove a bounded exact "
            "lifecycle; inspect quarantined artifacts before another T2 write "
            f"(t2_hard_stop={bool(capacity.get('t2_hard_stop'))})"
        )
    return {
        **result,
        "applied": str(result.get("status") or "") == "applied",
        "precheckpoint_reconciliation": precheckpoint_reconciliation,
        "next_checkpoint_budget": _next_checkpoint_budget(registry, result),
    }


def _next_checkpoint_budget(
    registry: WarehouseRecoveryRegistry, receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Warning only: observed last-three checkpoint bytes are not admission."""
    capacity = registry.capacity_status()
    estimate = int(receipt.get("projection", {}).get("recent_checkpoint_bytes") or 0)
    reserve = max(int(capacity["operational_reserve_bytes"]),
                  int(capacity["hard_stop_watermark_bytes"]))
    available = int(capacity["available_after_reservations_bytes"])
    known = estimate > 0
    headroom = available - reserve - estimate if known else None
    return {
        "status": "unknown" if not known else "warning" if headroom < 0 else "estimated_headroom",
        "reason_code": "checkpoint_estimate_unavailable" if not known else
                       "next_checkpoint_budget_insufficient" if headroom < 0 else "",
        "estimate_basis": "max_actual_bytes_last_three_retained_hourly_checkpoints",
        "checkpoint_estimate_bytes": estimate if known else None,
        "operational_reserve_bytes": reserve,
        "active_same_filesystem_reserved_bytes": int(capacity["reserved_bytes"]),
        "expired_reservation_count": int(capacity["expired_reservation_count"]),
        "available_after_reservations_bytes": available,
        "estimated_headroom_bytes": headroom,
        "admission_proven": False,
    }
