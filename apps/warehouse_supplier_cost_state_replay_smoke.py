#!/usr/bin/env python3
"""Regression smoke for append-only supplier certification replay."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional import (  # noqa: E402
    _effective_supplier_cost_states,
    _supplier_cost_allocations,
    _watermark,
    ensure_warehouse_functional_schema,
    load_supplier_line_cost_breakdown,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    WarehouseRecoveryRegistry,
)
from packages.application.warehouse_supplier_cost_state_replay import (  # noqa: E402
    WarehouseSupplierCostStateReplayError,
    apply_supplier_cost_state_replay_plan,
    build_supplier_cost_state_replay_plan,
    rollback_supplier_cost_state_replay,
    _frozen_supplier_states_from_version,
    _legacy_supplier_source_watermarks,
    _legacy_balance_proof_matches_allocation,
    _supplier_sources,
)


NOW = "2026-07-20T10:00:00Z"
VERSION_EFFECTIVE_AT = "2026-07-20T10:00:01Z"


def main() -> int:
    financial_documents = [
        {
            "supplier_order_id": "order-b",
            "document_date": "2026-01-01",
            "document_id": "document-1",
            "updated_at": "2026-07-20T09:00:00Z",
        },
        {
            "supplier_order_id": "order-a",
            "document_date": "2026-07-20",
            "document_id": "document-2",
            "updated_at": "2026-07-20T10:00:00Z",
        },
    ]
    supplier_watermarks = _legacy_supplier_source_watermarks(
        {
            "shipments": [],
            "cny_operations": [],
            "financial_documents": financial_documents,
        }
    )
    _assert(
        supplier_watermarks["financial_documents"]
        == _watermark(
            sorted(
                financial_documents,
                key=lambda row: (
                    row["document_date"],
                    row["document_id"],
                ),
            ),
            "updated_at",
        ),
        "legacy financial-document watermark reproduces functional source ordering",
    )

    with TemporaryDirectory(prefix="warehouse-supplier-certification-replay-") as raw:
        root = Path(raw)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed(runtime)

        before = load_supplier_line_cost_breakdown(runtime=runtime, shipment_id="26GN390")
        _assert(not before["certification"]["certified"], "missing projection fails closed")
        _assert(before["average_unit_cost_rub"] == "100", "canonical line cost remains visible")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            frozen = _frozen_supplier_states_from_version(
                conn,
                version_id="whfv_smoke",
            )["26GN390"]
            allocation = _supplier_cost_allocations(_supplier_sources(conn))["26GN390"]
        _assert(
            frozen["proof_kind"] == "legacy_balance_conservation",
            "fixture exercises the real pre-fingerprint version path",
        )
        _assert(
            _legacy_balance_proof_matches_allocation(frozen, allocation),
            "nested FF proof preserves legacy per-SKU capital and document identities",
        )
        mismatched_frozen = json.loads(json.dumps(frozen))
        mismatched_frozen["proof"]["balance_lines"]["391660889"]["capital_rub"] = "101"
        _assert(
            not _legacy_balance_proof_matches_allocation(mismatched_frozen, allocation),
            "legacy capital mismatch fails closed",
        )

        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_supplier_financial_documents(
                       document_id,supplier_order_id,document_type,original_filename,
                       stored_file_path,file_content_type,file_sha256,uploaded_at,updated_at,
                       parse_status,raw_parse_json,normalized_parse_json,warnings_json,errors_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "informational-contract-after-version",
                    "26GN390",
                    "contract",
                    "sanitized-contract.pdf",
                    "/sanitized/contract.pdf",
                    "application/pdf",
                    "sha256:sanitized",
                    "2026-07-20T10:01:00Z",
                    "2026-07-20T10:01:00Z",
                    "stored",
                    "{}",
                    "{}",
                    "[]",
                    "[]",
                ),
            )
            conn.commit()

        plan = build_supplier_cost_state_replay_plan(runtime, shipment_ids=["26GN390"])
        _assert(plan["correction_count"] == 1, "dry-run finds one bounded correction")
        _assert(
            plan["immutable_supplier_source_watermarks_match"] is False,
            "unrelated informational document changes only the global diagnostic watermark",
        )
        _assert(
            plan["corrections"][0]["shipment_id"] == "26GN390",
            "exact target conservation admits the unchanged legacy supplier flow",
        )
        _assert(
            plan["legacy_target_revision_proofs"]["26GN390"]["unchanged_since_version"]
            is True,
            "only source rows contributing to the target are revision-gated",
        )
        _assert(plan["corrections"][0]["supersedes_state_fingerprint"] == "missing", "supersedes is explicit")

        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_cny_ledger_operations
                   SET cny_delta='-11',updated_at=?
                   WHERE operation_id='payment-390'""",
                (VERSION_EFFECTIVE_AT,),
            )
            conn.commit()
        try:
            build_supplier_cost_state_replay_plan(runtime, shipment_ids=["26GN390"])
        except WarehouseSupplierCostStateReplayError as exc:
            _assert(
                "legacy_target_source_revision_after_version" in str(exc),
                "fingerprint-driving target drift fails even when RUB capital is unchanged",
            )
        else:
            raise AssertionError("target CNY source drift unexpectedly certified a legacy version")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_cny_ledger_operations
                   SET cny_delta='-10',updated_at=? WHERE operation_id='payment-390'""",
                (NOW,),
            )
            conn.commit()
        plan = build_supplier_cost_state_replay_plan(runtime, shipment_ids=["26GN390"])
        with mock.patch.object(
            runtime,
            "backup_database",
            side_effect=AssertionError("bounded replay reached full backup"),
        ) as full_backup:
            result = apply_supplier_cost_state_replay_plan(
                runtime,
                plan,
                confirm_fingerprint=plan["plan_fingerprint"],
                backup_dir=root / "backups",
            )
            full_backup.assert_not_called()
        _assert(
            result["recovery_policy"]["tier"] == "T1"
            and result["recovery_policy"]["lifecycle"] == "retained"
            and result["backup"]["copy_bytes"] == 0,
            "bounded replay uses the retained exact undo journal",
        )
        _assert(result["database_written"] is True, "first apply writes derived correction")
        _assert(result["primary_source_digest_before"] == result["primary_source_digest_after"], "primary source digest is conserved")
        certified = load_supplier_line_cost_breakdown(runtime=runtime, shipment_id="26GN390")
        _assert(certified["certification"]["certified"], "matching replay makes the closed shipment green")

        exact_repeat = apply_supplier_cost_state_replay_plan(
            runtime,
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        _assert(exact_repeat["idempotent"] is True, "exact repeated apply is a no-op")
        _assert(exact_repeat["database_written"] is False, "no-op creates no second backup/write")

        with mock.patch.object(
            runtime,
            "backup_database",
            side_effect=AssertionError("bounded rollback reached full backup"),
        ) as full_backup:
            rollback = rollback_supplier_cost_state_replay(
                runtime,
                replay_plan_fingerprint=plan["plan_fingerprint"],
                reason="smoke rollback proof",
                backup_dir=root / "backups",
            )
            full_backup.assert_not_called()
        _assert(rollback["database_written"] is True, "rollback appends a tombstone")
        repeated_rollback = rollback_supplier_cost_state_replay(
            runtime,
            replay_plan_fingerprint=plan["plan_fingerprint"],
            reason="smoke rollback proof",
            backup_dir=root / "backups",
        )
        _assert(
            repeated_rollback["idempotent"] is True
            and repeated_rollback["rollback_fingerprint"] == rollback["rollback_fingerprint"],
            "exact rollback retry is an idempotent no-op",
        )
        try:
            rollback_supplier_cost_state_replay(
                runtime,
                replay_plan_fingerprint=plan["plan_fingerprint"],
                reason="conflicting audit reason",
                backup_dir=root / "backups",
            )
        except WarehouseSupplierCostStateReplayError as exc:
            _assert("exact audit request" in str(exc), "conflicting rollback reason fails closed")
        else:
            raise AssertionError("conflicting rollback audit reason unexpectedly succeeded")
        rolled_back = load_supplier_line_cost_breakdown(runtime=runtime, shipment_id="26GN390")
        _assert(not rolled_back["certification"]["certified"], "rollback restores fail-closed state")
        with sqlite3.connect(runtime.db_path) as conn:
            _assert(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays"
                ).fetchone()[0]
                == 1,
                "rollback preserves replay audit",
            )
            _assert(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_supplier_cost_states"
                ).fetchone()[0]
                == 0,
                "immutable base version was never rewritten",
            )

        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_cny_ledger_operations
                   SET rub_value_delta='110'
                   WHERE operation_id='payment-390'"""
            )
            conn.commit()
        source_changed = load_supplier_line_cost_breakdown(runtime=runtime, shipment_id="26GN390")
        _assert(not source_changed["certification"]["certified"], "source mutation remains yellow")
        _assert(source_changed["average_unit_cost_rub"] == "112", "current canonical cost recalculates without zero")
        try:
            build_supplier_cost_state_replay_plan(runtime, shipment_ids=["26GN390"])
        except WarehouseSupplierCostStateReplayError as exc:
            _assert(
                "legacy_target_conservation_proof_mismatch" in str(exc),
                "changed source cannot certify stale immutable balances",
            )
        else:
            raise AssertionError("changed sources unexpectedly certified an old warehouse version")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_cny_ledger_operations
                   SET rub_value_delta='-98'
                   WHERE operation_id='payment-390'""",
            )
            conn.commit()

        replay_after_rollback = build_supplier_cost_state_replay_plan(
            runtime,
            shipment_ids=["26GN390"],
        )
        _assert(replay_after_rollback["replay_id"] != plan["replay_id"], "reapply has a new audit identity")
        apply_supplier_cost_state_replay_plan(
            runtime,
            replay_after_rollback,
            confirm_fingerprint=replay_after_rollback["plan_fingerprint"],
            backup_dir=root / "backups",
        )

        statements: list[str] = []
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.set_trace_callback(
                lambda statement: statements.append(statement)
                if statement.lstrip().upper().startswith("SELECT")
                else None
            )
            effective = _effective_supplier_cost_states(
                conn,
                version_id="whfv_smoke",
                shipment_ids=["26GN390", "missing-shipment"],
            )
        _assert(set(effective) == {"26GN390"}, "batch lookup returns only effective states")
        _assert(len(statements) == 2, "batch lookup uses one correction and one base query")

        rollback_supplier_cost_state_replay(
            runtime,
            replay_plan_fingerprint=replay_after_rollback["plan_fingerprint"],
            reason="prepare optimistic source-drift proof",
            backup_dir=root / "backups",
        )
        conflict_plan = build_supplier_cost_state_replay_plan(
            runtime,
            shipment_ids=["26GN390"],
        )
        original_begin_mutation = WarehouseRecoveryRegistry.begin_mutation

        def begin_then_drift(self, operation_id: str, **kwargs):
            evidence = original_begin_mutation(self, operation_id, **kwargs)
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    """UPDATE sheet_vitrina_v1_cny_ledger_operations
                       SET rub_value_delta='121',updated_at='2026-07-20T12:01:00Z'
                       WHERE operation_id='payment-390'"""
                )
                conn.commit()
            return evidence

        with mock.patch.object(
            WarehouseRecoveryRegistry,
            "begin_mutation",
            new=begin_then_drift,
        ):
            try:
                apply_supplier_cost_state_replay_plan(
                    runtime,
                    conflict_plan,
                    confirm_fingerprint=conflict_plan["plan_fingerprint"],
                    backup_dir=root / "backups",
                )
            except WarehouseSupplierCostStateReplayError as exc:
                _assert(
                    "legacy_target_source_revision_after_version" in str(exc),
                    "optimistic source conflict fails closed against immutable proof",
                )
            else:
                raise AssertionError("optimistic source conflict unexpectedly committed")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_cny_ledger_operations
                   SET rub_value_delta='-98',updated_at=?
                   WHERE operation_id='payment-390'""",
                (NOW,),
            )
            conn.commit()
        retry_plan = build_supplier_cost_state_replay_plan(runtime, shipment_ids=["26GN390"])
        retry = apply_supplier_cost_state_replay_plan(
            runtime,
            retry_plan,
            confirm_fingerprint=retry_plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        _assert(retry["database_written"] is True, "optimistic conflict remains retryable")

        with sqlite3.connect(runtime.db_path) as conn:
            replay_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays"
            ).fetchone()[0]
            correction_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections"
            ).fetchone()[0]
            rollback_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks"
            ).fetchone()[0]
        _assert((replay_count, correction_count, rollback_count) == (3, 3, 2), "append-only audit counts")
    print("warehouse_supplier_cost_state_replay_smoke: ok")
    return 0


def _seed(runtime: RegistryUploadDbBackedRuntime) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        ensure_warehouse_functional_schema(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_fulfillment_service_uploads(
                upload_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_fulfillment_service_lines(
                id TEXT PRIMARY KEY,upload_id TEXT NOT NULL,row_index INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_baseline_versions(
                baseline_id TEXT PRIMARY KEY,is_current INTEGER NOT NULL,report_json TEXT NOT NULL,
                primary_shipment_id TEXT NOT NULL,primary_accepted_ff_date TEXT NOT NULL,
                fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_baseline_lines(
                line_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_daily_state(
                as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,physical_quantity TEXT NOT NULL,
                stage TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions(
                   baseline_id,is_current,report_json,primary_shipment_id,
                   primary_accepted_ff_date,fingerprint
               ) VALUES('baseline-smoke',1,'{}','26GN390','2026-06-25','sha256:baseline')"""
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_supplier_shipments(
                   shipment_id,created_at,updated_at,shipment_date,actual_shipment_date,
                   order_status,expenses_complete,invoice_no,invoice_date,currency,
                   product_qty_total,product_amount_total,invoice_amount_total,
                   declared_invoice_total,match_status,warnings_json,errors_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "26GN390",
                NOW,
                NOW,
                "2026-06-20",
                "2026-06-25",
                "in_transit",
                1,
                "26GN390",
                "2026-06-20",
                "CNY",
                1,
                10,
                10,
                10,
                "matched",
                "[]",
                "[]",
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
                   line_id,shipment_id,line_type,sort_order,internal_nm_id,internal_name,
                   qty,unit_price,amount,currency,match_status,manual_override,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "line-390",
                "26GN390",
                "product",
                1,
                391660889,
                "Anti-Spy iPhone 16 Pro",
                1,
                10,
                10,
                "CNY",
                "matched",
                0,
                "{}",
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_cny_ledger_operations(
                   operation_id,operation_type,source_document_id,source_order_id,operation_date,operation_datetime,
                   sequence_key,cny_delta,rub_value_delta,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "payment-390",
                "supplier_payment_out",
                "payment-document-390",
                "26GN390",
                "2026-06-20",
                "2026-06-20T10:00:00Z",
                "2026-06-20T10:00:00Z:payment-390",
                "-10",
                "-98",
                "posted",
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_cny_documents(
                   document_id,document_type,source,source_order_id,context_order_id,
                   linked_financial_document_id,original_filename,stored_file_path,
                   file_content_type,file_sha256,natural_key,uploaded_at,created_at,
                   updated_at,operation_date,operation_datetime,status,document_number,
                   currency,rub_amount,cny_amount,bank_rate,parsed_payload_json,
                   raw_parse_json,parser_version,warnings_json,errors_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "payment-document-390",
                "supplier_cny_payment",
                "supplier_order",
                "26GN390",
                "26GN390",
                "",
                "payment-390.pdf",
                "",
                "application/pdf",
                "sha256:payment-390",
                "supplier_cny_payment:replay:390",
                NOW,
                NOW,
                NOW,
                "2026-06-20",
                "2026-06-20T10:00:00Z",
                "posted",
                "PAY-390",
                "CNY",
                "98",
                "10",
                "10",
                '{"document_number":"PAY-390"}',
                "{}",
                "fixture",
                "[]",
                "[]",
            ),
        )
        for document_id, document_type in (
            ("logistics-document-390", "logistics_invoice"),
            ("customs-document-390", "customs_declaration"),
        ):
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_supplier_financial_documents(
                       document_id,supplier_order_id,document_type,original_filename,
                       stored_file_path,file_content_type,file_sha256,uploaded_at,
                       updated_at,parse_status,raw_parse_json,normalized_parse_json,
                       warnings_json,errors_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    "26GN390",
                    document_type,
                    f"{document_id}.pdf",
                    "",
                    "application/pdf",
                    f"sha256:{document_id}",
                    NOW,
                    NOW,
                    "confirmed",
                    "{}",
                    "{}",
                    "[]",
                    "[]",
                ),
            )
        for index, document_id, category in (
            (1, "logistics-document-390", "logistics"),
            (2, "customs-document-390", "customs_fee_1010"),
        ):
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines(
                       line_id,financial_document_id,supplier_order_id,sort_order,
                       category,stage,description,amount,currency,amount_rub,
                       included_in_logistics_efficiency,included_in_customs_total,
                       status,confidence,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"expense-line-{index}-390",
                    document_id,
                    "26GN390",
                    index,
                    category,
                    "china_to_ff",
                    "Fixture confirmed cost component",
                    1,
                    "RUB",
                    1,
                    0,
                    0,
                    "confirmed",
                    1,
                    "{}",
                ),
            )
        from packages.application.supplier_financial_documents import (
            supplier_payment_fee_fingerprint,
        )

        payment_fingerprint = supplier_payment_fee_fingerprint(
            {
                "document_id": "payment-document-390",
                "operation_date": "2026-06-20",
                "cny_amount": "10",
                "currency": "CNY",
            }
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_supplier_payment_fee_confirmations(
                   confirmation_id,supplier_order_id,payment_document_id,
                   payment_fingerprint,confirmation_type,status,reason,actor,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "zero-fee-payment-document-390",
                "26GN390",
                "payment-document-390",
                payment_fingerprint,
                "zero_fee",
                "active",
                "Fixture: банк подтвердил отсутствие комиссии",
                "warehouse-smoke",
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                   version_id,cutover_id,version_kind,effective_at,status,plan_fingerprint,
                   local_source_digest,source_watermarks_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "whfv_smoke",
                "warehouse_functional_cutover_v1",
                "hourly_wb_sync",
                VERSION_EFFECTIVE_AT,
                "good",
                "sha256:immutable-version",
                "sha256:legacy-source-capture",
                "{}",
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at)
               VALUES(1,'whfv_smoke',?)""",
            (NOW,),
        )
        frozen_source = {
            "supplier_flow_id": "supplier:26GN390",
            "shipment_id": "26GN390",
            "flow_quantity": "1",
            "flow_capital_rub": "100",
            "quality": "certified",
            "expenses_complete_certification": True,
            "invoice_no": "26GN390",
            "invoice_date": "2026-06-20",
            "actual_shipment_date": "2026-06-25",
            "payment_operation_ids": ["payment-390"],
            "cny_fee_operation_ids": [],
            "direct_rub_bank_fees": "0",
            "china_expense_sources": [
                "logistics-document-390:expense-line-1-390",
                "customs-document-390:expense-line-2-390",
            ],
            "allocation": (
                "supplier/payment/bank fee by invoice value; logistics/1010 by quantity; "
                "2010/5010 by invoice value"
            ),
        }
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                   version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client,provenance_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "whfv_smoke",
                "ff",
                391660889,
                "1",
                "100",
                "100",
                "1",
                "certified",
                1,
                "0",
                "0",
                "0",
                json.dumps(
                    {"source_records": [frozen_source]},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        supplier_watermarks = _legacy_supplier_source_watermarks(
            _supplier_sources(conn)
        )
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_versions
               SET local_source_digest=?,source_watermarks_json=?
               WHERE version_id='whfv_smoke'""",
            (
                "sha256:legacy-version-wide-digest-can-advance-independently",
                json.dumps(supplier_watermarks, sort_keys=True, separators=(",", ":")),
            ),
        )
        # A later derived-history publication legitimately changes the broad
        # functional source set, but not any supplier source or frozen balance.
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_canonical_cost_daily_state(
                   as_of_date,nm_id,physical_quantity,stage
               ) VALUES('2026-07-20',391660889,'1','WB')"""
        )
        conn.commit()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
