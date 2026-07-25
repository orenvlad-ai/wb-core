#!/usr/bin/env python3
"""Executable contract for the unified recovery durable step journal."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_cost_unified_recovery import (  # noqa: E402
    _checkpoint_audit,
    _bank_plan,
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
    _assert_cny_ledger_payment_anchors()
    _assert_audit_waits_for_sqlite_writer()
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


def _assert_cny_ledger_payment_anchors() -> None:
    with TemporaryDirectory(prefix="warehouse-cost-bank-anchor-") as temp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp) / "runtime"
        )
        runtime.save_supplier_shipment(
            header={
                "shipment_id": "shipment",
                "created_at": "2026-07-17T08:00:00Z",
                "updated_at": "2026-07-17T08:00:00Z",
                "shipment_date": "2026-07-17",
                "invoice_no": "26GN527",
                "invoice_date": "2026-07-17",
                "contract_no": "082/26",
                "currency": "CNY",
            },
            lines=[],
        )
        for operation_number, amount in (
            ("7", "59921.25"),
            ("11", "339553.75"),
        ):
            runtime.save_cny_document(
                {
                    "document_id": f"cny-payment-{operation_number}",
                    "document_type": "supplier_cny_payment",
                    "source": "smoke",
                    "source_order_id": "shipment",
                    "natural_key": f"payment-{operation_number}",
                    "uploaded_at": "2026-07-17T08:00:00Z",
                    "created_at": "2026-07-17T08:00:00Z",
                    "updated_at": "2026-07-17T08:00:00Z",
                    "operation_date": "2026-07-17",
                    "status": "posted",
                    "document_number": operation_number,
                    "currency": "CNY",
                    "cny_amount": amount,
                    "parsed_payload": {
                        "document_number": operation_number,
                        "cny_amount": amount,
                        "invoice_number": "26GN527",
                    },
                }
            )
        operations = [
            _payment_row("7", "59921.25"),
            _payment_row("11", "339553.75"),
        ]
        fee_rows = [
            _fee_row(
                "cc-7",
                operation_number="7",
                amount="948.60",
                category="currency_control_fee",
                bank_document_number="130623",
                operation_date="2026-06-30",
            ),
            _fee_row(
                "transfer-7",
                operation_number="7",
                amount="13668.11",
                category="bank_transfer_fee",
                bank_document_number="443906",
                operation_date="2026-06-30",
            ),
            _fee_row(
                "cc-11",
                operation_number="11",
                amount="4788.83",
                category="currency_control_fee",
                bank_document_number="50149",
                operation_date="2026-07-20",
            ),
            _fee_row(
                "transfer-11-a",
                operation_number="11",
                amount="20000",
                category="bank_transfer_fee",
                bank_document_number="244189",
                operation_date="2026-07-20",
            ),
            _fee_row(
                "transfer-11-b",
                operation_number="11",
                amount="58113.66",
                category="bank_transfer_fee",
                bank_document_number="244189",
                operation_date="2026-07-21",
            ),
        ]
        runtime.save_supplier_financial_document(
            document={
                "document_id": "statement",
                "supplier_order_id": "shipment",
                "document_type": "bank_fee_statement",
                "file_sha256": "b" * 64,
                "uploaded_at": "2026-07-24T08:00:00Z",
                "updated_at": "2026-07-24T08:00:00Z",
                "parse_status": "confirmed",
                "normalized_parse": {
                    "document_type": "bank_fee_statement",
                    "operations": [*operations, *fee_rows],
                    "fee_rows": fee_rows,
                },
            },
            expense_lines=[],
        )
        args = argparse.Namespace(
            shipment_id="shipment",
            statement_document_id="statement",
            commission_amount=["20000", "58113.66"],
            expected_logical_fee_count=4,
            expected_atomic_fee_count=5,
            expected_bank_total="97519.20",
        )
        shipment = runtime.load_supplier_shipment("shipment") or {}
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            plan = _bank_plan(
                conn,
                runtime=runtime,
                args=args,
                shipment=shipment,
            )
        if (
            plan.get("amount") != "78113.66"
            or plan.get("logical_fee_count") != 4
            or plan.get("atomic_fee_count") != 5
            or plan.get("total_rub") != "97519.20"
            or len(plan.get("new_atomic_operation_ids") or []) != 2
            or plan.get("atomic_business_dates")
            != ["2026-07-20", "2026-07-21"]
        ):
            raise AssertionError(
                f"CNY-ledger supplier payments must anchor recovery preview: {plan}"
            )
        revision_before = str(plan.get("target_revision") or "")
        changed = runtime.load_cny_document("cny-payment-11") or {}
        runtime.save_cny_document(
            {
                **changed,
                "updated_at": "2026-07-17T08:01:00Z",
            }
        )
        from packages.application.supplier_financial_documents import (
            SupplierFinancialDocumentsBlock,
        )

        revision_after = SupplierFinancialDocumentsBlock(
            runtime=runtime
        )._bank_fee_preview_revision(
            "shipment",
            statement_file_sha256="b" * 64,
            exclude_document_id="statement",
        )
        if not revision_before or revision_before == revision_after:
            raise AssertionError(
                "CNY payment revision must stale the bank confirmation preview"
            )


def _assert_audit_waits_for_sqlite_writer() -> None:
    with TemporaryDirectory(prefix="warehouse-cost-audit-lock-") as temp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp) / "runtime"
        )
        plan = _plan()
        _ensure_audit_schema(runtime)
        _start_audit(runtime, plan)
        writer_started = threading.Event()

        def hold_unrelated_write() -> None:
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                writer_started.set()
                time.sleep(0.15)
                conn.commit()

        writer = threading.Thread(target=hold_unrelated_write)
        writer.start()
        assert writer_started.wait(timeout=2)
        elapsed_started = time.monotonic()
        wait_ms = _checkpoint_audit(
            runtime,
            plan,
            {"bank": {"idempotent": True}},
        )
        elapsed_ms = (time.monotonic() - elapsed_started) * 1000
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert wait_ms >= 100, wait_ms
        assert elapsed_ms >= 100, elapsed_ms
        assert (
            _load_audit_record(runtime, FINGERPRINT) or {}
        ).get("steps") == {"bank": {"idempotent": True}}
        from apps import warehouse_cost_unified_recovery as recovery

        blocker = sqlite3.connect(runtime.db_path)
        blocker.execute("BEGIN IMMEDIATE")
        prior_wait_ms = recovery.AUDIT_SQLITE_LOCK_WAIT_MS
        recovery.AUDIT_SQLITE_LOCK_WAIT_MS = 1
        try:
            try:
                _checkpoint_audit(
                    runtime,
                    plan,
                    {"bank": {"idempotent": True}, "box": {}},
                )
            except RuntimeError as exc:
                assert str(exc) == (
                    "unified_recovery_sqlite_write_wait_expired"
                )
                assert "database is locked" not in str(exc)
            else:
                raise AssertionError(
                    "audit checkpoint ignored bounded SQLite writer wait"
                )
        finally:
            recovery.AUDIT_SQLITE_LOCK_WAIT_MS = prior_wait_ms
            blocker.rollback()
            blocker.close()


def _payment_row(operation_number: str, amount: str) -> dict:
    return {
        "row_id": f"payment-row-{operation_number}",
        "row_type": "supplier_payment",
        "operation_number": operation_number,
        "debit_cny": amount,
        "amount": amount,
        "operation_date": "2026-07-17",
        "invoice_number": "26GN527",
    }


def _fee_row(
    row_id: str,
    *,
    operation_number: str,
    amount: str,
    category: str,
    bank_document_number: str,
    operation_date: str,
) -> dict:
    cny_amount = "59921.25" if operation_number == "7" else "339553.75"
    return {
        "row_id": row_id,
        "row_type": "bank_fee",
        "operation_number": operation_number,
        "amount": amount,
        "debit_rub": amount,
        "currency": "RUB",
        "fee_category": category,
        "bank_document_number": bank_document_number,
        "operation_date": operation_date,
        "invoice_number": "26GN527",
        "amount_in_purpose_cny": cny_amount,
        "payment_purpose": (
            f"Комиссия по платежу №{operation_number} на сумму {cny_amount} CNY. "
            + (
                "Включая НДС."
                if category == "currency_control_fee"
                else "НДС не облагается."
            )
        ),
        "semantic_operation_id": f"bankop-{row_id}",
    }


if __name__ == "__main__":
    main()
