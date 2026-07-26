"""Guarded one-supply ФФ ledger reconciliation for WB supply 40561872.

The default mode is a read-only dry-run. Apply and audited reversal both require
the exact fingerprint from a fresh matching dry-run and a coherent SQLite backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_stock_ledger import (  # noqa: E402
    FfStockLedgerBlock,
    TargetedWbSupplyReconciliationError,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    DB_FILENAME,
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryState,
    WarehouseRecoveryRegistry,
)


TARGET_SUPPLY_ID = "40561872"


def main() -> int:
    args = _parse_args()
    runtime_dir = Path(
        args.runtime_dir
        or os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR")
        or ROOT / ".runtime" / "registry_upload"
    )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    block = FfStockLedgerBlock(runtime=runtime)
    report: dict[str, object] = {
        "runner": "ff_stock_targeted_reconciliation",
        "scope": {"supply_id": TARGET_SUPPLY_ID, "bulk_apply_supported": False},
        "runtime_db": str(runtime_dir / DB_FILENAME),
        "mode": "reversal" if args.reversal else "reconciliation",
        "requested_action": "apply" if args.apply else "dry_run",
        "runtime_mutation_performed": False,
    }
    try:
        if args.reversal:
            plan = block.plan_targeted_wb_supply_reversal(args.supply_id)
        else:
            plan = block.plan_targeted_wb_supply_reconciliation(args.supply_id)
        report["preflight"] = plan
        if not args.apply:
            report["status"] = str(plan.get("status") or "dry_run")
            _print_report(report, compact=args.compact)
            return 0 if plan.get("apply_allowed") or plan.get("idempotent") else 2

        if not args.confirm_fingerprint:
            raise TargetedWbSupplyReconciliationError(
                "confirmation_fingerprint_required",
                "--apply requires --confirm-fingerprint from the current dry-run",
            )
        if not args.backup_dir:
            raise TargetedWbSupplyReconciliationError(
                "backup_dir_required",
                "--apply requires an explicit --backup-dir",
            )
        if plan.get("idempotent"):
            if args.confirm_fingerprint != str(plan.get("fingerprint") or ""):
                raise TargetedWbSupplyReconciliationError(
                    "stale_or_invalid_fingerprint",
                    "Provided fingerprint does not match the existing idempotent operation",
                    details={
                        "expected": str(plan.get("fingerprint") or ""),
                        "provided": args.confirm_fingerprint,
                    },
                )
            report["status"] = str(plan.get("status") or "already_applied")
            report["result"] = plan
            report["recovery_policy"] = WarehouseRecoveryRegistry(
                runtime_dir=runtime.runtime_dir,
                db_path=runtime.db_path,
            ).plan_noop(
                mutation_kind="ff_ledger_operation",
                closure_kind="shipment",
                plan_fingerprint=args.confirm_fingerprint,
                scope={"supply_id": args.supply_id, "reversal": args.reversal},
            )
            _print_report(report, compact=args.compact)
            return 0
        if args.confirm_fingerprint != str(plan.get("fingerprint") or ""):
            raise TargetedWbSupplyReconciliationError(
                "stale_or_invalid_fingerprint",
                "Provided fingerprint does not match the current preflight",
                details={
                    "expected": str(plan.get("fingerprint") or ""),
                    "provided": args.confirm_fingerprint,
                },
            )
        if not plan.get("apply_allowed"):
            raise TargetedWbSupplyReconciliationError(
                "targeted_reconciliation_blocked",
                "Current preflight is blocked and cannot be applied",
                details=plan.get("blockers") or [],
            )
        operation_id = "ffso_recovery_" + hashlib.sha256(
            args.confirm_fingerprint.encode("utf-8")
        ).hexdigest()[:20]
        recovery_registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime.runtime_dir,
            db_path=runtime.db_path,
        )
        recovery = recovery_registry.prepare_t1(
            mutation_kind="ff_ledger_operation",
            closure_kind="shipment",
            plan_fingerprint=args.confirm_fingerprint,
            scope={
                "supply_id": args.supply_id,
                "reversal": args.reversal,
                "operation_id": operation_id,
                "nm_ids": sorted(
                    {
                        int(item.get("nm_id") or 0)
                        for item in (
                            (plan.get("apply_guards") or {}).get(
                                "operation_lines"
                            )
                            or []
                        )
                    }
                ),
            },
            before_images=[
                {
                    "table": "sheet_vitrina_v1_ff_stock_operations",
                    "key": {"operation_id": operation_id},
                    "before": None,
                    "after": None,
                }
            ],
            expected_after_images=[
                {
                    "table": "sheet_vitrina_v1_ff_stock_operations",
                    "key": {"operation_id": operation_id},
                    "source_key": (
                        f"wb_supply_debit_reversal:supply:{args.supply_id}"
                        if args.reversal
                        else f"wb_supply_debit:supply:{args.supply_id}"
                    ),
                    "operation_lines": list(
                        (plan.get("apply_guards") or {}).get("operation_lines")
                        or []
                    ),
                }
            ],
            source_digest=args.confirm_fingerprint,
        )
        if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
            recovery = recovery_registry.begin_mutation(
                str(recovery["operation_id"]),
                expected_source_digest=args.confirm_fingerprint,
            )
        try:
            if args.reversal:
                result = block.apply_targeted_wb_supply_reversal(
                    args.supply_id,
                    apply=True,
                    confirmation_fingerprint=args.confirm_fingerprint,
                    created_by=args.created_by,
                    operation_id=operation_id,
                )
            else:
                result = block.apply_targeted_wb_supply_reconciliation(
                    args.supply_id,
                    apply=True,
                    confirmation_fingerprint=args.confirm_fingerprint,
                    created_by=args.created_by,
                    operation_id=operation_id,
                )
        except Exception as exc:
            recovery_registry.fail_recoverable(
                str(recovery["operation_id"]),
                error=str(exc),
                next_action="resume_or_rollback_targeted_ff_ledger",
            )
            raise
        recovery = recovery_registry.retain(
            str(recovery["operation_id"]),
            after_digest=str(result.get("fingerprint") or args.confirm_fingerprint),
        )
        report["backup"] = {
            "kind": "target_scoped_before_image",
            "operation_id": recovery["operation_id"],
            "full_database_copy": False,
            "copy_bytes": 0,
        }
        report["recovery_policy"] = recovery
        report["status"] = str(result.get("status") or "applied")
        report["result"] = result
        report["runtime_mutation_performed"] = True
        report["reversibility"] = {
            "history_deletion_allowed": False,
            "command_mode": "--reversal",
            "requires_fresh_fingerprint_and_t1_journal": True,
        }
        _print_report(report, compact=args.compact)
        return 0
    except TargetedWbSupplyReconciliationError as exc:
        report.update(
            {
                "status": "blocked",
                "error": {"code": exc.code, "message": str(exc), "details": exc.details},
            }
        )
    except ValueError as exc:
        report.update(
            {
                "status": "blocked",
                "error": {"code": "atomic_apply_guard_failed", "message": str(exc)},
            }
        )
    _print_report(report, compact=args.compact)
    return 2


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="", help=f"Runtime dir containing {DB_FILENAME}.")
    parser.add_argument("--supply-id", required=True, help=f"Must be exactly {TARGET_SUPPLY_ID}.")
    parser.add_argument("--apply", action="store_true", help="Apply the current plan; omitted means read-only dry-run.")
    parser.add_argument("--confirm-fingerprint", default="", help="Exact sha256 fingerprint from the current dry-run.")
    parser.add_argument("--backup-dir", default="", help="Deprecated compatibility argument; recovery is policy-managed.")
    parser.add_argument("--created-by", default="operator", help="Audit actor stored on the ledger operation.")
    parser.add_argument("--reversal", action="store_true", help="Plan/apply an audited compensating receipt; never deletes history.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON instead of indented JSON.")
    args = parser.parse_args()
    args.supply_id = str(args.supply_id or "").strip()
    if args.supply_id != TARGET_SUPPLY_ID:
        parser.error(f"this bounded runner accepts only --supply-id {TARGET_SUPPLY_ID}")
    return args


def _print_report(report: dict[str, object], *, compact: bool) -> None:
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
            default=str,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
