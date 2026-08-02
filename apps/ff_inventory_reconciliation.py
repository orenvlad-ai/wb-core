"""Guarded FF physical-inventory and missing-supply reconciliation runner.

Dry-run is the default.  Apply requires the exact fresh plan fingerprint, an
explicit production approval reference and a policy-managed T1 journal.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_inventory_reconciliation import (  # noqa: E402
    FfInventoryReconciliation,
    FfInventoryReconciliationError,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    DB_FILENAME,
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryState,
    WarehouseRecoveryRegistry,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


def main() -> int:
    args = _parse_args()
    runtime_dir = Path(
        args.runtime_dir
        or os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR")
        or ROOT / ".runtime" / "registry_upload"
    )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    block = FfInventoryReconciliation(runtime=runtime)
    source_bytes = _load_source(args)
    source_sha256 = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    report: dict[str, object] = {
        "runner": "ff_inventory_reconciliation",
        "contract_name": "ff_inventory_reconciliation_v1",
        "requested_action": "rollback" if args.rollback else "readback" if args.readback else "apply" if args.apply else "dry_run",
        "runtime_db": str(runtime.db_path),
        "runtime_mutation_performed": False,
        "scope": {
            "business_date": args.business_date,
            "source_sha256": source_sha256,
            "return_supply_ids": sorted(args.return_supply_id),
        },
    }
    try:
        if args.rollback:
            if not args.confirm_fingerprint or not args.approval_reference or not args.rollback_reason:
                raise FfInventoryReconciliationError(
                    "rollback_authority_required",
                    "--rollback requires --confirm-fingerprint, --approval-reference and --rollback-reason",
                )
            with warehouse_functional_write_lock(runtime.runtime_dir):
                result = block.rollback(
                    confirmation_fingerprint=args.confirm_fingerprint,
                    approval_reference=args.approval_reference,
                    reason=args.rollback_reason,
                    created_by=args.created_by,
                )
            report.update(
                {
                    "status": result.get("status"),
                    "result": result,
                    "runtime_mutation_performed": not bool(result.get("idempotent")),
                    "reversibility": {
                        "recovery_tier": "T1",
                        "method": "append_only_compensating_documents",
                        "source_history_deleted": False,
                    },
                }
            )
            _print(report, compact=args.compact)
            return 0
        if args.readback:
            result = block.readback(
                source_sha256=source_sha256,
                business_date=args.business_date,
            )
            report.update({"status": result.get("status"), "result": result})
            _print(report, compact=args.compact)
            return 0 if str(result.get("status")) == "applied" else 2

        plan = block.build_plan(
            source_bytes=source_bytes,
            source_filename=args.source_filename,
            business_date=args.business_date,
            return_supply_ids=args.return_supply_id,
        )
        report["plan"] = plan
        if not args.apply:
            report["status"] = str(plan.get("status") or "dry_run")
            _print(report, compact=args.compact)
            return 0 if plan.get("apply_allowed") or plan.get("idempotent") else 2
        if not args.confirm_fingerprint:
            raise FfInventoryReconciliationError(
                "confirmation_fingerprint_required",
                "--apply requires --confirm-fingerprint from the exact current dry-run",
            )
        if not args.approval_reference:
            raise FfInventoryReconciliationError(
                "approval_reference_required",
                "--apply requires --approval-reference from the human production gate",
            )
        if args.confirm_fingerprint != str(plan.get("fingerprint") or ""):
            raise FfInventoryReconciliationError(
                "stale_or_invalid_fingerprint",
                "Provided fingerprint does not match the current dry-run",
            )
        if not plan.get("apply_allowed") and not plan.get("idempotent"):
            raise FfInventoryReconciliationError(
                "plan_blocked",
                "Current plan is blocked and cannot be applied",
                details=plan.get("blockers") or [],
            )
        result, recovery, plan = _apply_locked(
            runtime=runtime,
            block=block,
            args=args,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
        )
        report["plan"] = plan
        report.update(
            {
                "status": str(result.get("status") or "applied"),
                "result": result,
                "recovery_policy": recovery,
                "runtime_mutation_performed": not bool(result.get("idempotent")),
                "reversibility": {
                    "history_deletion_allowed": False,
                    "recovery_tier": "T1",
                    "compensating_documents_required": True,
                },
            }
        )
        _print(report, compact=args.compact)
        return 0
    except FfInventoryReconciliationError as exc:
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
    _print(report, compact=args.compact)
    return 2


def _apply_locked(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    block: FfInventoryReconciliation,
    args: argparse.Namespace,
    source_bytes: bytes,
    source_sha256: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Rebuild the exact plan and retain T1 evidence under the shared writer lock."""

    with warehouse_functional_write_lock(runtime.runtime_dir):
        plan = block.build_plan(
            source_bytes=source_bytes,
            source_filename=args.source_filename,
            business_date=args.business_date,
            return_supply_ids=args.return_supply_id,
        )
        if args.confirm_fingerprint != str(plan.get("fingerprint") or ""):
            raise FfInventoryReconciliationError(
                "stale_or_invalid_fingerprint",
                "Provided fingerprint does not match the locked current dry-run",
            )
        if not plan.get("apply_allowed") and not plan.get("idempotent"):
            raise FfInventoryReconciliationError(
                "plan_blocked",
                "Locked current plan is blocked and cannot be applied",
                details=plan.get("blockers") or [],
            )
        if plan.get("idempotent"):
            result = block.apply_plan(
                source_bytes=source_bytes,
                source_filename=args.source_filename,
                business_date=args.business_date,
                return_supply_ids=args.return_supply_id,
                confirmation_fingerprint=args.confirm_fingerprint,
                approval_reference=args.approval_reference,
                created_by=args.created_by,
            )
            return (
                dict(result),
                {
                    "lifecycle": "T0",
                    "mutation_kind": "ff_inventory_reconciliation",
                    "plan_fingerprint": args.confirm_fingerprint,
                    "runtime_mutation_performed": False,
                },
                dict(plan),
            )
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime.runtime_dir,
            db_path=runtime.db_path,
        )
        manifest = dict(plan.get("manifest") or {})
        operation_ids = list(manifest.get("expected_operation_ids") or [])
        recovery = registry.prepare_t1(
            mutation_kind="ff_inventory_reconciliation",
            closure_kind="sku_date",
            plan_fingerprint=args.confirm_fingerprint,
            scope={
                "business_date": args.business_date,
                "source_sha256": source_sha256,
                "nm_ids": sorted(
                    int(item.get("nm_id") or 0)
                    for item in manifest.get("per_sku") or []
                ),
                "operation_ids": operation_ids,
            },
            before_images=[
                {
                    "table": "sheet_vitrina_v1_ff_stock_operations",
                    "key": {"operation_id": operation_id},
                    "before": None,
                    "after": None,
                }
                for operation_id in operation_ids
            ],
            expected_after_images=[
                {
                    "table": "sheet_vitrina_v1_ff_stock_operations",
                    "key": {"operation_id": document.get("operation_id")},
                    "source_key": document.get("source_key"),
                    "operation_type": document.get("operation_type"),
                    "line_count": len(document.get("lines") or []),
                }
                for document in manifest.get("documents") or []
            ],
            source_digest=source_sha256,
        )
        if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
            recovery = registry.begin_mutation(
                str(recovery["operation_id"]),
                expected_source_digest=source_sha256,
            )
        try:
            result = block.apply_plan(
                source_bytes=source_bytes,
                source_filename=args.source_filename,
                business_date=args.business_date,
                return_supply_ids=args.return_supply_id,
                confirmation_fingerprint=args.confirm_fingerprint,
                approval_reference=args.approval_reference,
                created_by=args.created_by,
            )
        except Exception as exc:
            registry.fail_recoverable(
                str(recovery["operation_id"]),
                error=str(exc),
                next_action="resume_or_append_inventory_compensation",
            )
            raise
        recovery = registry.retain(
            str(recovery["operation_id"]),
            after_digest=str(result.get("after_digest") or args.confirm_fingerprint),
            non_target_digest=str(result.get("non_target_digest") or ""),
        )
    return dict(result), dict(recovery), dict(plan)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-file", default="")
    source.add_argument("--source-base64-stdin", action="store_true")
    parser.add_argument("--source-filename", required=True)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--return-supply-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--readback", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--rollback-reason", default="")
    parser.add_argument("--confirm-fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--created-by", default="operator")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if sum(bool(item) for item in (args.apply, args.readback, args.rollback)) > 1:
        parser.error("--apply, --readback and --rollback are mutually exclusive")
    if len(str(args.business_date)) != 10:
        parser.error("--business-date must be YYYY-MM-DD")
    return args


def _load_source(args: argparse.Namespace) -> bytes:
    if args.source_base64_stdin:
        encoded = sys.stdin.read().strip()
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise FfInventoryReconciliationError(
                "invalid_source_base64",
                "Source stdin is not valid base64",
            ) from exc
    return Path(args.source_file).read_bytes()


def _print(report: Mapping[str, object], *, compact: bool) -> None:
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
