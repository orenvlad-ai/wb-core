#!/usr/bin/env python3
"""Executable contract for the unified recovery durable step journal."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_cost_unified_recovery import (  # noqa: E402
    _checkpoint_audit,
    _ensure_audit_schema,
    _load_audit_record,
    _mark_audit_failed,
    _save_audit,
    _start_audit,
    _validate_resume_invariants,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


FINGERPRINT = "sha256:" + "a" * 64


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-cost-unified-recovery-") as temp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp) / "runtime"
        )
        plan = _plan()
        _ensure_audit_schema(runtime)
        _start_audit(runtime, plan)
        _checkpoint_audit(
            runtime,
            plan,
            {"bank": {"idempotent": False, "atomic_rows": 2}},
        )
        _mark_audit_failed(
            runtime,
            plan,
            {"bank": {"idempotent": False, "atomic_rows": 2}},
            RuntimeError("injected interruption"),
        )
        failed = _load_audit_record(runtime, FINGERPRINT) or {}
        assert failed["status"] == "failed"
        assert failed["steps"]["bank"]["atomic_rows"] == 2
        assert failed["plan"]["performance"]["full_database_copy"] is False

        _start_audit(runtime, failed["plan"])
        _validate_resume_invariants(plan, _current_plan())
        resumed_steps = {
            **failed["steps"],
            "physical": {"supply_count": 4, "idempotent": True},
        }
        _checkpoint_audit(runtime, plan, resumed_steps)
        report = {
            "plan_fingerprint": FINGERPRINT,
            "applied_at": "2026-07-25T12:00:00Z",
            "steps": resumed_steps,
            "performance": {
                "copy_bytes": 0,
                "finance_raw_rows_read": 0,
                "full_database_copy": False,
            },
        }
        _save_audit(runtime, report)
        complete = _load_audit_record(runtime, FINGERPRINT) or {}
        assert complete["status"] == "complete"
        assert complete["report"]["performance"]["copy_bytes"] == 0

        drifted = _current_plan()
        drifted["physical"]["supplies"][0]["source_revision"] = "sha256:drift"
        try:
            _validate_resume_invariants(plan, drifted)
        except ValueError as exc:
            assert "supply revisions changed" in str(exc)
        else:
            raise AssertionError("resume accepted drifted WB evidence")
    print("warehouse_cost_unified_recovery_smoke: OK")


def _plan() -> dict:
    return {
        "fingerprint": FINGERPRINT,
        "scope": {
            "shipment_id": "shipment",
            "invoice_no": "26GN527",
            "statement_document_id": "statement",
            "supply_ids": ["supply-1"],
            "box_supply_id": "box-supply",
            "affected_nm_ids": [101, 102],
        },
        "shipment": {
            "planned_date": "2026-07-17",
            "actual_date_before": "",
            "actual_date_after": "2026-07-21",
        },
        "physical": {
            "supplies": [
                {
                    "supply_id": "supply-1",
                    "source_revision": "sha256:goods",
                }
            ]
        },
        "box": {"source_revision": "sha256:box-goods"},
        "performance": {
            "copy_bytes": 0,
            "finance_raw_rows_read": 0,
            "full_database_copy": False,
        },
    }


def _current_plan() -> dict:
    current = _plan()
    current["fingerprint"] = "sha256:" + "b" * 64
    current["shipment"] = {
        **current["shipment"],
        "actual_date_before": "2026-07-21",
        "would_change": False,
    }
    current["physical"]["supplies"][0]["already_debited"] = True
    current["box"]["already_applied"] = True
    return current


if __name__ == "__main__":
    main()
