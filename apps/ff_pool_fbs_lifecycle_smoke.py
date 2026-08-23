#!/usr/bin/env python3
"""Exact historical and post-T FBS lifecycle integration smoke."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_cutover_production_smoke import (  # noqa: E402
    GATE_AT,
    SHA,
    SHIPMENT_ID,
    _Clock,
    _barrier,
    _seed,
)
from packages.application.ff_pool_cutover import read_ff_pool_cutover_status  # noqa: E402
from packages.application.ff_pool_cutover_production import (  # noqa: E402
    FfPoolCutoverProductionMutation,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    DRAIN_STATE_TABLE,
    EVENTS_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    RECONCILIATION_TABLE,
    available_quantity,
    process_post_t_fbs_lifecycle,
)
from packages.application.canonical_rub_money import (  # noqa: E402
    compare_canonical_rub_money,
)
from packages.application.ff_pool_foundation import read_ff_pool_feature_state  # noqa: E402
from packages.application.ff_pool_documents import (  # noqa: E402
    FfPoolDocumentService,
    _guided_request_source_revision,
)
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.contracts.ff_pool_documents import DocumentIdentity  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional import (  # noqa: E402
    ensure_warehouse_functional_schema,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    WbFbsOrdersCollector,
    _current_cte,
)


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True)
        runtime.save_nomenclature_item(
            {
                "item_id": "ff-pool-lifecycle-nm-101",
                "is_active": True,
                "is_hidden": False,
                "our_sku": "seller-101",
                "nm_id": 101,
                "barcode": "sku-101",
                "nomenclature_name": "Lifecycle SKU 101",
                "created_at": GATE_AT,
                "updated_at": GATE_AT,
            }
        )
        runtime.save_nomenclature_item(
            {
                "item_id": "ff-pool-lifecycle-nm-103",
                "is_active": True,
                "is_hidden": False,
                "our_sku": "seller-103",
                "nm_id": 103,
                "barcode": "sku-103",
                "nomenclature_name": "New inbound SKU 103",
                "created_at": GATE_AT,
                "updated_at": GATE_AT,
            }
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_warehouse_functional_schema(conn)
            _ensure_schema(conn)
            _seed(conn)
            _append_status(
                conn,
                order_id=9001,
                revision="order_revision_9001",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-14T04:04:00Z",
                insert_current=False,
            )
            _append_status(
                conn,
                order_id=9001,
                revision="order_revision_9001_cancelled",
                supplier_status="complete",
                wb_status="canceled_by_client",
                episode=3,
                observed_at="2026-08-14T04:05:00Z",
                insert_current=False,
            )
            conn.commit()
        env_file = root / "runtime.env"
        env_file.write_text("WB_FBS_COLLECTOR_ENABLED=true\n", encoding="utf-8")
        runner = FfPoolCutoverProductionMutation(
            runtime_dir=runtime_dir,
            env_file=env_file,
            deployed_sha=SHA,
            timestamp_factory=_Clock(),
        )
        gate = runner.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
        historical = gate["source"]["historical_fbs_summary"]
        assert historical["counts"]["pre_t_handoff_debit"] == 1
        assert historical["quantities"]["pre_t_handoff_debit"] == 1
        assert historical["debit_capital_rub"] == "10"
        assert historical["post_handoff_reconciliation_count"] == 1
        applied = runner.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-gate-lifecycle",
            actor="smoke",
            backup_dir=root / "backups",
            external_barrier_evidence=_barrier(),
        )
        assert applied["status"] == "applied_reconciled"
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT quantity FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()[0] == 9
            assert conn.execute(
                "SELECT quantity FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"
            ).fetchone()[0] == "9"
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE event_type='opening_handoff_debit'"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} "
                "WHERE event_type='post_handoff_reconciliation' AND order_id=9001"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {RECONCILIATION_TABLE} WHERE order_id=9001"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_cutover_order_status_evidence "
                "WHERE order_id=9001"
            ).fetchone()[0] == 2

        # Eleven active post-T orders create reservations only.  Available may
        # be negative, but physical and capital remain untouched.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for order_id in range(9200, 9211):
                _insert_post_t_order(conn, order_id=order_id, supplier="new", wb="waiting")
            conn.commit()
        before_wb = _wb_evidence_digest(runtime.db_path)
        processed = _process(runtime.db_path, "2026-08-14T06:10:00Z")
        assert processed["summary"]["reserved"] == 11
        assert _wb_evidence_digest(runtime.db_path) == before_wb
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            available = available_quantity(
                conn, cutover_id=_cutover_id(conn), facility_id="fac_moscow", nm_id=101
            )
        assert available == {"physical": 9, "reserved": 11, "available": -2}
        orders = WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-14T06:10:00Z",
            enabled=False,
        )
        reserved_order = orders.orders_page(search="9202")
        assert reserved_order["page"]["total"] == 1
        assert reserved_order["rows"][0]["reservation"] == {
            "state": "reserved",
            "quantity": 1,
            "active": True,
            "updated_at": "2026-08-14T06:10:00Z",
        }

        # A quantity correction before handoff refreshes the exact reservation
        # from immutable status evidence without changing physical stock.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9202,
                revision="post_revision_9202_v2",
                supplier_status="new",
                wb_status="waiting",
                episode=2,
                observed_at="2026-08-14T06:10:30Z",
                insert_current=True,
                quantity=3,
            )
            conn.commit()
        refreshed = _process(runtime.db_path, "2026-08-14T06:10:45Z")
        assert refreshed["summary"]["reservation_refreshed"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            available = available_quantity(
                conn, cutover_id=_cutover_id(conn), facility_id="fac_moscow", nm_id=101
            )
            assert available == {"physical": 9, "reserved": 13, "available": -4}

        # A pre-handoff cancellation releases only the reservation.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9200,
                revision="post_revision_9200_v2",
                supplier_status="cancel",
                wb_status="waiting",
                episode=2,
                observed_at="2026-08-14T06:11:00Z",
                insert_current=True,
            )
            conn.commit()
        before_wb = _wb_evidence_digest(runtime.db_path)
        released = _process(runtime.db_path, "2026-08-14T06:12:00Z")
        assert released["summary"]["released"] == 1
        assert _wb_evidence_digest(runtime.db_path) == before_wb

        # WB-controlled complete/sorted fulfills once with frozen opening WAC.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v2",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-14T06:13:00Z",
                insert_current=True,
            )
            conn.commit()
        handed = _process(runtime.db_path, "2026-08-14T06:14:00Z")
        assert handed["summary"]["fulfilled"] == 1
        handed_page = orders.orders_page(search="9201", status_category="handed_over")
        assert handed_page["page"]["total"] == 1
        handed_row = handed_page["rows"][0]
        assert handed_row["reservation"]["state"] == "fulfilled"
        assert handed_row["debit_close_evidence"]["event_type"] == "handoff_debit"
        assert handed_row["debit_close_evidence"]["event_digest"].startswith("sha256:")
        handed_detail = orders.order_detail(9201)
        assert handed_detail["current"]["status_category"] == "handed_over"
        assert handed_detail["lifecycle"]["state"] == "fulfilled"
        assert any(
            item["event_type"] == "handoff_debit"
            for item in handed_detail["lifecycle_evidence"]
        )
        with sqlite3.connect(runtime.db_path) as conn:
            physical = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            assert tuple(physical) == (8, "80")
            operation_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0]
            feature = read_ff_pool_feature_state(conn, aggregate_revision="wf_stage7c")
            assert feature.reader_effective is True

        # Later sold/closed is a no-op; later cancellation is evidence for a
        # separate reconciliation lane and never silently returns stock.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v3",
                supplier_status="complete",
                wb_status="sold",
                episode=3,
                observed_at="2026-08-14T06:15:00Z",
                insert_current=True,
            )
            conn.commit()
        terminal = _process(runtime.db_path, "2026-08-14T06:16:00Z")
        assert terminal["summary"]["terminal_noop"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0] == operation_count
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v4",
                supplier_status="complete",
                wb_status="canceled_by_client",
                episode=4,
                observed_at="2026-08-14T06:17:00Z",
                insert_current=True,
            )
            conn.commit()
        reconciled = _process(runtime.db_path, "2026-08-14T06:18:00Z")
        assert reconciled["summary"]["reconciliation"] == 1
        reconciliation_page = orders.orders_page(
            search="9201", status_category="reconciliation"
        )
        assert reconciliation_page["page"]["total"] == 1
        assert reconciliation_page["rows"][0]["reconciliation_evidence"]["digest"].startswith(
            "sha256:"
        )
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {RECONCILIATION_TABLE}").fetchone()[0] == 2
            assert conn.execute(
                "SELECT quantity FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()[0] == 8

        # Reordered waiting→sorted evidence after fulfillment is consumed by
        # exact status sequence but never creates a second physical debit.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v5",
                supplier_status="complete",
                wb_status="waiting",
                episode=5,
                observed_at="2026-08-14T06:18:30Z",
                insert_current=True,
            )
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v6",
                supplier_status="complete",
                wb_status="sorted",
                episode=6,
                observed_at="2026-08-14T06:18:40Z",
                insert_current=True,
            )
            conn.commit()
        reordered = _process(runtime.db_path, "2026-08-14T06:19:00Z")
        assert reordered["summary"]["status_noop"] == 2
        duplicate_retry = _process(runtime.db_path, "2026-08-14T06:19:30Z")
        assert duplicate_retry["processed_count"] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0] == operation_count

        # A late-arriving order locally observed at/before T is isolated;
        # it neither double-debits nor globally blocks post-T processing.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn,
                order_id=9300,
                supplier="complete",
                wb="sorted",
                source_created_at="2026-08-14T05:04:00Z",
                observed_at=GATE_AT,
            )
            conn.commit()
        late = _process(runtime.db_path, "2026-08-14T06:20:00Z")
        assert late["summary"]["late_pre_t"] == 1
        repeated = _process(runtime.db_path, "2026-08-14T06:21:00Z")
        assert repeated["summary"]["fulfilled"] == 0
        assert repeated["summary"]["late_pre_t"] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            status = read_ff_pool_cutover_status(conn)
            assert status["readback"]["status"] == "pass"
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases"
            ).fetchone()[0] == 1

        # The manifest-pinned shipment remains in transit through cutover and
        # can later be accepted exactly once by the guided Migration 139 flow.
        # Exact invoice evidence may arrive after opening.  It is still not a
        # receipt/cost layer and therefore does not retroactively change the
        # cutover; it gives the guided acceptance an exact supplier-capital
        # basis for the two physically received units.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipments
                   SET approx_yuan_rate='10',
                       product_qty_total='66000',
                       product_amount_total='66000',
                       extras_amount_total='0',
                       invoice_amount_total='66000',
                       updated_at='2026-08-15T04:00:00Z'
                   WHERE shipment_id=?""",
                (SHIPMENT_ID,),
            )
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipment_lines
                   SET qty=65999, unit_price='1', amount='65999', currency='CNY',
                       invoice_price_yuan_snapshot='1',
                       reference_purchase_price_yuan_snapshot='1'
                   WHERE shipment_id=? AND line_type='product'""",
                (SHIPMENT_ID,),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
                       line_id,shipment_id,line_type,sort_order,internal_nm_id,qty,
                       unit_price,amount,currency,invoice_price_yuan_snapshot,
                       reference_purchase_price_yuan_snapshot,manual_override,
                       price_conformity_status,price_conformity_check_mode,
                       price_conformity_reason,price_conformity_context_json,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "line_26gn527_2", SHIPMENT_ID, "product", 2, 103, 1,
                    "1", "1", "CNY", "1", "1", 0,
                    "not_checked", "not_checked", "not_checked", "{}", "{}",
                ),
            )
            conn.commit()
        doc_clock = _DocClock()
        service = FfPoolDocumentService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=doc_clock,
        )
        _shipment, _shipment_lines, shipment_revision = FfPoolSurface(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=_DocClock(),
        ).supplier_shipment_source(SHIPMENT_ID)
        guided_source_bytes = b"guided-26gn527-workbook-evidence"
        guided_source_sha256 = "sha256:" + hashlib.sha256(
            guided_source_bytes
        ).hexdigest()
        guided_request_revision = _guided_request_source_revision(
            supplier_source_revision=shipment_revision,
            source_sha256=guided_source_sha256,
        )
        identity = DocumentIdentity(
            request_id="guided:26gn527:request",
            source_system="operator_ui",
            source_type="china_acceptance_workbook",
            source_id=SHIPMENT_ID,
            source_revision=guided_request_revision,
            idempotency_epoch=1,
            actor="warehouse-operator",
            business_date="2026-08-15",
        )
        acceptance_manifest = {
            "facility_id": "fac_moscow",
            "source_revision": shipment_revision,
            "allocations": [
                {
                    "nm_id": 101,
                    "expected_quantity": 65_999,
                    "accepted_quantity": 2,
                    "quantity_fbs": 1,
                    "quantity_fbo": 1,
                    "accepted_capital_rub": "20.0049",
                    "discrepancy_type": "shortage",
                    "discrepancy_quantity": 65_997,
                    "identity_evidence_digest": "sha256:" + "9" * 64,
                },
                {
                    "nm_id": 103,
                    "expected_quantity": 1,
                    "accepted_quantity": 1,
                    "quantity_fbs": 1,
                    "quantity_fbo": 0,
                    "accepted_capital_rub": "10.0049",
                    "discrepancy_type": "none",
                    "discrepancy_quantity": 0,
                    "identity_evidence_digest": "sha256:" + "8" * 64,
                },
            ],
            "expenses": [
                {
                    "amount_rub": "2.00",
                    "basis": "Фактическая приёмка",
                    "metadata": {"allocation_scope": "both"},
                }
            ],
        }
        preview = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert preview["state"] == "ready", preview
        assert preview["confirm_allowed"] is True
        assert preview["preview_manifest"]["posting_plan_preview"][
            "confirm_plan_ready"
        ] is True
        assert preview["preview_manifest"]["posting_plan_preview"][
            "aggregate_semantic_zero_nm_ids"
        ] == [103]
        assert preview["preview_manifest"]["capital_normalization"] == {
            "policy": "header_round_half_up_then_largest_fractional_remainder_nm_id",
            "exact_total_rub": "30.0098",
            "canonical_total_rub": "30.01",
            "total_residual_rub": "0.0002",
            "residual_owner_nm_ids": [101],
            "capital_cents_by_nm": {"101": 2001, "103": 1000},
            "normalization_residual_rub_by_nm": {
                "101": "0.0051",
                "103": "-0.0049",
            },
        }
        # A pre-upgrade ready request is not confirmable until the identical
        # immutable source is retried and the complete query-only plan is
        # durably proven in place.
        with sqlite3.connect(runtime.db_path) as conn:
            legacy_ready_manifest = dict(preview["preview_manifest"])
            legacy_ready_manifest.pop("posting_plan_preview")
            conn.execute(
                """UPDATE sheet_vitrina_v1_ff_pool_document_requests
                   SET preview_manifest_json=? WHERE request_id=?""",
                (
                    json.dumps(
                        legacy_ready_manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    str(preview["request_id"]),
                ),
            )
            conn.commit()
        assert service.status(request_id=str(preview["request_id"]))[
            "confirm_allowed"
        ] is False
        upgraded_ready = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert upgraded_ready["request_id"] == preview["request_id"]
        assert upgraded_ready["confirm_allowed"] is True
        assert upgraded_ready["preview_manifest"]["posting_plan_preview"][
            "aggregate_semantic_zero_nm_ids"
        ] == [103]
        # The first production readiness release compared the current raw
        # supplier revision with the combined raw+workbook request revision.
        # An exact request blocked by that defect is reopened in place only
        # when both stored bindings recompute; no duplicate or business row is
        # created.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_ff_pool_document_requests
                   SET state='blocked',error_code='supplier_source_revision_changed',
                       preview_manifest_json=?
                   WHERE request_id=?""",
                (
                    json.dumps(
                        legacy_ready_manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    str(preview["request_id"]),
                ),
            )
            conn.commit()
        source_contract_retry = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert source_contract_retry["request_id"] == preview["request_id"]
        assert source_contract_retry["state"] == "ready"
        assert source_contract_retry["confirm_allowed"] is True
        assert source_contract_retry["idempotent"] is True
        assert source_contract_retry["preview_manifest"]["posting_plan_preview"][
            "confirm_plan_ready"
        ] is True
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                """SELECT COUNT(*) FROM sheet_vitrina_v1_ff_workflow_events
                   WHERE identity=? AND stage='source_revision_contract_revalidation'
                     AND status='complete'""",
                (str(preview["request_id"]),),
            ).fetchone()[0] == 1
            assert conn.execute(
                """SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_document_requests
                   WHERE request_id=?""",
                (str(preview["request_id"]),),
            ).fetchone()[0] == 1
            assert conn.execute(
                """SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_documents
                   WHERE request_id=?""",
                (str(preview["request_id"]),),
            ).fetchone()[0] == 0
        # Reopening the compatibility state never weakens the live source
        # check.  Even an internal caller that repeats the old identity is
        # blocked again while supplier truth is different.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipments
                   SET updated_at='2026-08-15T04:00:01Z'
                   WHERE shipment_id=?""",
                (SHIPMENT_ID,),
            )
            conn.execute(
                """UPDATE sheet_vitrina_v1_ff_pool_document_requests
                   SET state='blocked',error_code='supplier_source_revision_changed'
                   WHERE request_id=?""",
                (str(preview["request_id"]),),
            )
            conn.commit()
        drifted_contract_retry = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert drifted_contract_retry["state"] == "blocked"
        assert drifted_contract_retry["error"]["code"] == (
            "supplier_source_revision_changed"
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipments
                   SET updated_at='2026-08-15T04:00:00Z'
                   WHERE shipment_id=?""",
                (SHIPMENT_ID,),
            )
            conn.commit()
        restored_contract_retry = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert restored_contract_retry["state"] == "ready"
        assert restored_contract_retry["confirm_allowed"] is True
        assert restored_contract_retry["preview_manifest"]["posting_plan_preview"][
            "aggregate_pool_parity"
        ]["status"] == "pass"
        # Global aggregate/detail parity is a readiness and confirm boundary,
        # not merely a post-mutation assertion.  A stale hourly aggregate
        # therefore blocks before T1/business writes, and the same immutable
        # request may reopen only after exact parity is restored.
        with sqlite3.connect(runtime.db_path) as conn:
            before_aggregate = conn.execute(
                """SELECT quantity,capital_rub FROM
                          sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"""
            ).fetchone()
            assert before_aggregate is not None
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET quantity=CAST(quantity AS INTEGER)+1
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"""
            )
            conn.commit()
        parity_block = service.post(str(preview["request_id"]))
        assert parity_block["state"] == "blocked"
        assert parity_block["error"]["code"] == "guided_acceptance_parity_not_current"
        parity_repeat_while_stale = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert parity_repeat_while_stale["state"] == "blocked"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET quantity=?,capital_rub=?
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101""",
                (str(before_aggregate[0]), str(before_aggregate[1])),
            )
            conn.commit()
        parity_reopened = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert parity_reopened["state"] == "ready"
        assert parity_reopened["confirm_allowed"] is True
        assert parity_reopened["request_id"] == preview["request_id"]
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                """SELECT COUNT(*) FROM sheet_vitrina_v1_ff_workflow_events
                   WHERE identity=? AND stage='aggregate_parity_revalidation'
                     AND status='complete'""",
                (str(preview["request_id"]),),
            ).fetchone()[0] == 1
            assert conn.execute(
                """SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_documents
                   WHERE request_id=?""",
                (str(preview["request_id"]),),
            ).fetchone()[0] == 0

        # The confirm path must reproduce the exact durable preview, including
        # its aggregate/pool proof, both before and while holding the apply
        # lock.  A corrupted/stale stored proof blocks with no business row.
        with sqlite3.connect(runtime.db_path) as conn:
            stored = json.loads(
                conn.execute(
                    """SELECT preview_manifest_json FROM
                              sheet_vitrina_v1_ff_pool_document_requests
                       WHERE request_id=?""",
                    (str(preview["request_id"]),),
                ).fetchone()[0]
            )
            stored["posting_plan_preview"]["business_effect_sha256"] = (
                "sha256:" + "0" * 64
            )
            conn.execute(
                """UPDATE sheet_vitrina_v1_ff_pool_document_requests
                   SET preview_manifest_json=? WHERE request_id=?""",
                (
                    json.dumps(
                        stored,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    str(preview["request_id"]),
                ),
            )
            conn.commit()
        plan_drift_block = service.post(str(preview["request_id"]))
        assert plan_drift_block["state"] == "blocked"
        assert plan_drift_block["error"]["code"] == (
            "guided_acceptance_posting_plan_drift"
        )
        plan_reopened = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert plan_reopened["state"] == "ready"
        assert plan_reopened["confirm_allowed"] is True
        assert plan_reopened["preview_manifest"]["posting_plan_preview"][
            "business_effect_sha256"
        ] != "sha256:" + "0" * 64
        # A production request blocked by the former minor-unit check is
        # reopened in place; the immutable request/workbook is not duplicated.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_ff_pool_document_requests
                   SET state='blocked',error_code='money_minor_unit_required'
                   WHERE request_id=?""",
                (str(preview["request_id"]),),
            )
            conn.commit()
        retried_preview = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest=acceptance_manifest,
            source_bytes=guided_source_bytes,
        )
        assert retried_preview["request_id"] == preview["request_id"]
        assert retried_preview["state"] == "ready"
        assert retried_preview["idempotent"] is True
        stable_business_effect = retried_preview["preview_manifest"][
            "posting_plan_preview"
        ]["business_effect_sha256"]
        preview_posted_manifest = retried_preview["preview_manifest"][
            "posting_plan_preview"
        ]["posted_manifest_sha256"]

        # Ordinary FBS work is allowed to advance after the owner-facing
        # preview.  Reservations/releases change dependent evidence, while a
        # handoff debit changes both pool detail and the active aggregate in
        # one transaction.  None of these events changes the receipt effect.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn, order_id=9450, supplier="new", wb="waiting"
            )
            _insert_post_t_order(
                conn, order_id=9451, supplier="new", wb="waiting"
            )
            conn.commit()
        moving_reservations = _process(
            runtime.db_path, "2026-08-15T08:10:00Z"
        )
        assert moving_reservations["summary"]["reserved"] == 2
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9450,
                revision="post_revision_9450_v2",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-15T08:10:10Z",
                insert_current=True,
            )
            _append_status(
                conn,
                order_id=9451,
                revision="post_revision_9451_v2",
                supplier_status="cancel",
                wb_status="waiting",
                episode=2,
                observed_at="2026-08-15T08:10:11Z",
                insert_current=True,
            )
            conn.commit()
        moving_suffix = _process(runtime.db_path, "2026-08-15T08:10:20Z")
        assert moving_suffix["summary"]["fulfilled"] == 1
        assert moving_suffix["summary"]["released"] == 1
        assert _process(runtime.db_path, "2026-08-15T08:10:30Z")[
            "processed_count"
        ] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            pool_after_suffix = conn.execute(
                """SELECT quantity,capital_rub FROM
                          sheet_vitrina_v1_ff_pool_balances
                   WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"""
            ).fetchone()
            aggregate_after_suffix = conn.execute(
                """SELECT quantity,capital_rub FROM
                          sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"""
            ).fetchone()
            assert tuple(pool_after_suffix) == (7, "70")
            assert tuple(aggregate_after_suffix) == ("7", "70")

        doc_clock.value = datetime(2026, 8, 15, 8, 11, tzinfo=timezone.utc)
        posted = service.post(str(preview["request_id"]))
        assert posted["state"] == "complete", posted
        assert posted["posted_manifest_sha256"] != preview_posted_manifest
        assert retried_preview["preview_manifest"]["posting_plan_preview"][
            "business_effect_sha256"
        ] == stable_business_effect
        repeated_acceptance = service.post(str(preview["request_id"]))
        assert repeated_acceptance["state"] == "complete"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            shipment = conn.execute(
                "SELECT actual_ff_acceptance_date,order_status FROM sheet_vitrina_v1_supplier_shipments "
                "WHERE shipment_id=?",
                (SHIPMENT_ID,),
            ).fetchone()
            assert tuple(shipment) == ("2026-08-15", "accepted_ff")
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                "WHERE source_key=?",
                (f"supplier_shipment_acceptance:{SHIPMENT_ID}",),
            ).fetchone()[0] == 1
            fbs = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            fbo = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101"
            ).fetchone()
            new_fbs = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=103"
            ).fetchone()
            assert int(fbs[0]) == 8 and Decimal(str(fbs[1])) == Decimal("80.67")
            assert int(fbo[0]) == 1 and Decimal(str(fbo[1])) == Decimal("10.68")
            assert int(new_fbs[0]) == 1 and Decimal(str(new_fbs[1])) == Decimal("10.66")
            new_aggregate = conn.execute(
                """SELECT quantity,capital_rub,cost_covered_quantity,quality,certified
                   FROM sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=103"""
            ).fetchone()
            assert tuple(new_aggregate) == (
                "1", "10.66", "1", "guided_acceptance_minor_unit", 0,
            )
            post_acceptance_feature = read_ff_pool_feature_state(
                conn, aggregate_revision="wf_stage7c"
            )
            assert post_acceptance_feature.reader_effective is True, (
                post_acceptance_feature
            )
            replay = conn.execute(
                """SELECT legacy_operation_id,cost_layer_id,capital_normalization_json
                   FROM sheet_vitrina_v1_ff_guided_acceptance_replays
                   WHERE request_id=?""",
                (str(preview["request_id"]),),
            ).fetchone()
            assert replay is not None
            assert json.loads(replay[2])["canonical_total_rub"] == "30.01"
            receipt_snapshot = json.loads(
                conn.execute(
                    """SELECT raw_json FROM sheet_vitrina_v1_ff_stock_operation_lines
                       WHERE operation_id=? AND nm_id=101""",
                    (str(replay[0]),),
                ).fetchone()[0]
            )["cost_snapshot"]
            assert receipt_snapshot["capital_delta_rub"] == "20.01"
            assert receipt_snapshot["quality"] == "guided_acceptance_minor_unit"
            guided_readback = read_ff_pool_cutover_status(conn)["readback"]
            assert guided_readback["status"] == "pass", guided_readback

        # Recovery freezes cost coverage and metadata as well as headline
        # quantity/capital.  Any affected-field drift blocks before mutation.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET cost_covered_quantity='0.5'
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=103"""
            )
            conn.commit()
        coverage_drift_preview = service.accept_preview(
            identity=DocumentIdentity(
                request_id="guided:26gn527:coverage-drift-recovery",
                source_system="operator_ui",
                source_type="guided_acceptance_recovery",
                source_id=str(posted["document"]["document_id"]),
                source_revision="sha256:" + "5" * 64,
                idempotency_epoch=1,
                actor="warehouse-operator",
                business_date="2026-08-15",
            ),
            document_kind="storno",
            manifest={"target_document_id": str(posted["document"]["document_id"])},
        )
        coverage_drift_result = service.post(
            str(coverage_drift_preview["request_id"])
        )
        assert coverage_drift_result["state"] == "blocked"
        assert coverage_drift_result["error"]["code"] == "guided_recovery_aggregate_drift"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET cost_covered_quantity='1'
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=103"""
            )
            conn.commit()

        recovery_identity = DocumentIdentity(
            request_id="guided:26gn527:recovery",
            source_system="operator_ui",
            source_type="guided_acceptance_recovery",
            source_id=str(posted["document"]["document_id"]),
            source_revision="sha256:" + "7" * 64,
            idempotency_epoch=1,
            actor="warehouse-operator",
            business_date="2026-08-15",
        )
        recovery_preview = service.accept_preview(
            identity=recovery_identity,
            document_kind="storno",
            manifest={"target_document_id": str(posted["document"]["document_id"])},
        )
        assert recovery_preview["state"] == "ready"
        recovered = service.post(str(recovery_preview["request_id"]))
        assert recovered["state"] == "complete", recovered
        with sqlite3.connect(runtime.db_path) as conn:
            supplier_after_recovery = conn.execute(
                """SELECT actual_ff_acceptance_date,order_status
                   FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?""",
                (SHIPMENT_ID,),
            ).fetchone()
            assert tuple(supplier_after_recovery) == (None, "in_transit")
            recovered_pool = conn.execute(
                """SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances
                   WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"""
            ).fetchone()
            assert int(recovered_pool[0]) == 7
            assert Decimal(str(recovered_pool[1])) == Decimal("70")
            recovered_new_aggregate = conn.execute(
                """SELECT quantity,wac_rub,capital_rub,cost_covered_quantity,quality
                   FROM sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=103"""
            ).fetchone()
            assert tuple(recovered_new_aggregate) == (
                "0", None, "0", "0", "guided_acceptance_minor_unit",
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_guided_acceptance_recoveries"
            ).fetchone()[0] == 1
            recovery_snapshot = json.loads(
                conn.execute(
                    """SELECT line.raw_json
                       FROM sheet_vitrina_v1_ff_stock_operation_lines AS line
                       JOIN sheet_vitrina_v1_ff_stock_operations AS operation
                         ON operation.operation_id=line.operation_id
                       WHERE operation.source_type='supplier_shipment_acceptance_recovery'
                         AND line.nm_id=101"""
                ).fetchone()[0]
            )["cost_snapshot"]
            assert Decimal(recovery_snapshot["capital_delta_rub"]) == Decimal("-20.01")
            assert recovery_snapshot["quality"] == "guided_acceptance_recovery"

        # Recovery leaves the shipment reusable through a fresh source
        # revision; the recovered immutable request itself never reactivates.
        _shipment, _shipment_lines, replacement_revision = FfPoolSurface(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=_DocClock(),
        ).supplier_shipment_source(SHIPMENT_ID)
        replacement_manifest = dict(acceptance_manifest)
        replacement_manifest["source_revision"] = replacement_revision
        replacement_source_bytes = b"guided-26gn527-replacement-workbook"
        replacement_request_revision = _guided_request_source_revision(
            supplier_source_revision=replacement_revision,
            source_sha256="sha256:"
            + hashlib.sha256(replacement_source_bytes).hexdigest(),
        )
        replacement_identity = DocumentIdentity(
            request_id="guided:26gn527:replacement",
            source_system="operator_ui",
            source_type="china_acceptance_workbook",
            source_id=SHIPMENT_ID,
            source_revision=replacement_request_revision,
            idempotency_epoch=1,
            actor="warehouse-operator",
            business_date="2026-08-15",
        )
        replacement_preview = service.accept_preview(
            identity=replacement_identity,
            document_kind="china_acceptance",
            manifest=replacement_manifest,
            source_bytes=replacement_source_bytes,
        )
        assert replacement_preview["state"] == "ready"
        replacement_posted = service.post(str(replacement_preview["request_id"]))
        assert replacement_posted["state"] == "complete", replacement_posted

        # Quantity comes from immutable official status evidence; it is never
        # approximated as one unit merely because one order row is present.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn, order_id=9400, supplier="new", wb="waiting", quantity=3
            )
            conn.commit()
        exact_reserved = _process(runtime.db_path, "2026-08-15T08:10:00Z")
        assert exact_reserved["summary"]["reserved"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9400,
                revision="post_revision_9400_v2",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-15T08:11:00Z",
                insert_current=True,
                quantity=3,
            )
            conn.commit()
        exact_handoff = _process(runtime.db_path, "2026-08-15T08:12:00Z")
        assert exact_handoff["summary"]["fulfilled"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            exact_balance = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            assert int(exact_balance[0]) == 5
            assert Decimal(str(exact_balance[1])) == Decimal("50.41875")
            handoff_event = conn.execute(
                "SELECT frozen_wac_rub,details_json FROM "
                "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
                "WHERE order_id=9400 AND event_type='handoff_debit'"
            ).fetchone()
            assert Decimal(str(handoff_event[0])) == Decimal("10.08375")
            assert json.loads(str(handoff_event[1]))["cost_basis"]["contract"] == (
                "fbs_handoff_current_facility_wac_v1"
            )
            final_readback = read_ff_pool_cutover_status(conn)["readback"]
            assert final_readback["status"] == "pass", final_readback

            # Reproduce the production capital shape: aggregate parity is
            # exact, but one pool owns a tail beyond process-default Decimal
            # precision.  A later FBS debit must conserve that tail in both
            # pool detail and aggregate rather than rounding each base at a
            # different significant digit.
            tail = Decimal("0.000000000000000000000000000000000000000000000001")
            fbo_balance = conn.execute(
                "SELECT capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101"
            ).fetchone()
            aggregate_balance = conn.execute(
                "SELECT capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"
            ).fetchone()
            with localcontext() as context:
                context.prec = 160
                tailed_fbs = Decimal(str(exact_balance[1])) + tail
                tailed_fbo = Decimal(str(fbo_balance[0])) - tail
                assert tailed_fbs + tailed_fbo == Decimal(str(aggregate_balance[0]))
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_pool_balances SET capital_rub=? "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101",
                (str(tailed_fbs),),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_pool_balances SET capital_rub=? "
                "WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101",
                (str(tailed_fbo),),
            )
            # Mirror the production writer history: aggregate text differs by
            # a raw 10^-23 tail while both sides are the same canonical kopeck.
            # The lifecycle gate must retain that evidence but not block an
            # otherwise exact debit.
            aggregate_raw_tail = Decimal(
                "0.00000000000000000000004"
            )
            tailed_aggregate = Decimal(str(aggregate_balance[0])) + aggregate_raw_tail
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                "SET capital_rub=? WHERE version_id='wf_stage7c' "
                "AND warehouse_key='ff' AND nm_id=101",
                (format(tailed_aggregate, "f"),),
            )
            conn.commit()

        # An unrelated post-T order without exact identity evidence is
        # isolated instead of pinning the global suffix cursor forever.  A
        # later matched order still reserves exactly once, while the isolated
        # handoff produces no guessed physical/capital effect.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn,
                order_id=9410,
                supplier="complete",
                wb="sorted",
                observed_at="2026-08-15T08:13:00Z",
                quantity=2,
                identity_outcome="unmatched_identity",
            )
            _insert_post_t_order(
                conn,
                order_id=9411,
                supplier="new",
                wb="waiting",
                observed_at="2026-08-15T08:13:01Z",
            )
            balance_before_identity_pending = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            conn.commit()
        wb_before_identity_pending = _wb_evidence_digest(runtime.db_path)
        isolated_identity = _process(runtime.db_path, "2026-08-15T08:13:10Z")
        assert isolated_identity["status"] == "caught_up_identity_pending"
        assert isolated_identity["processed_count"] == 2
        assert isolated_identity["summary"]["identity_pending"] == 1
        assert isolated_identity["summary"]["reserved"] == 1
        assert isolated_identity["identity_pending_count"] == 1
        assert _wb_evidence_digest(runtime.db_path) == wb_before_identity_pending
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} WHERE order_id=9410"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {IDENTITY_PENDING_RESOLUTIONS_TABLE}"
            ).fetchone()[0] == 0
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9410"
            ).fetchone()[0] == 0
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9411 "
                "AND event_type='reserve'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone() == balance_before_identity_pending

        repeated_identity_pending = _process(
            runtime.db_path, "2026-08-15T08:13:20Z"
        )
        assert repeated_identity_pending["processed_count"] == 0
        assert repeated_identity_pending["identity_retry_count"] == 1
        assert repeated_identity_pending["identity_pending_count"] == 1
        assert repeated_identity_pending["summary"]["identity_pending"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id IN (9410,9411)"
            ).fetchone()[0] == 1

            # Mapping evidence is append-only.  Once the collector can prove
            # the exact same order revision, the original pending handoff is
            # replayed; it is not replaced with a synthetic current status.
            revision_9410 = str(
                conn.execute(
                    "SELECT source_revision FROM "
                    "sheet_vitrina_v1_wb_supplies_fbs_order_observations "
                    "WHERE order_id=9410 ORDER BY observation_sequence DESC LIMIT 1"
                ).fetchone()[0]
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
                       evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                       barcode,seller_sku,outcome,warehouse_mapping_id,
                       identity_mapping_id,evidence_digest,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "post_identity_evidence_9410_matched",
                    9410,
                    revision_9410,
                    501,
                    101,
                    201,
                    "sku-101",
                    "seller-101",
                    "matched",
                    "warehouse_mapping_1",
                    "identity_mapping_1",
                    "sha256:"
                    + hashlib.sha256(b"identity:9410:matched").hexdigest(),
                    "2026-08-15T08:13:25Z",
                ),
            )
            conn.commit()

        wb_before_identity_resolution = _wb_evidence_digest(runtime.db_path)
        resolved_identity = _process(runtime.db_path, "2026-08-15T08:13:30Z")
        assert resolved_identity["status"] == "caught_up"
        assert resolved_identity["processed_count"] == 0
        assert resolved_identity["identity_retry_count"] == 1
        assert resolved_identity["identity_pending_count"] == 0
        assert resolved_identity["summary"]["identity_resolved"] == 1
        assert resolved_identity["summary"]["fulfilled"] == 1
        assert _wb_evidence_digest(runtime.db_path) == wb_before_identity_resolution
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {IDENTITY_PENDING_RESOLUTIONS_TABLE} "
                "WHERE order_id=9410 AND resolution_kind='matched_replay'"
            ).fetchone()[0] == 1
            debit_event = conn.execute(
                f"SELECT physical_quantity_delta,capital_delta_rub FROM {EVENTS_TABLE} "
                "WHERE order_id=9410 AND event_type='handoff_debit'"
            ).fetchone()
            assert tuple(debit_event) == (
                -2,
                "-20.1675000000000000000000000000000000000000000000004",
            )
            balance_after_identity_resolution = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            fbo_after_identity_resolution = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101"
            ).fetchone()
            aggregate_after_identity_resolution = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"
            ).fetchone()
            assert int(balance_after_identity_resolution[0]) == int(
                balance_before_identity_pending[0]
            ) - 2
            with localcontext() as context:
                context.prec = 160
                expected_capital = Decimal(
                    str(balance_before_identity_pending[1])
                ) - Decimal(
                    "20.1675000000000000000000000000000000000000000000004"
                )
                detail_capital_after_resolution = (
                    Decimal(str(balance_after_identity_resolution[1]))
                    + Decimal(str(fbo_after_identity_resolution[1]))
                )
            raw_tail_parity = compare_canonical_rub_money(
                detail_capital_after_resolution,
                aggregate_after_identity_resolution[1],
                left_field="production-shaped detail capital",
                right_field="production-shaped aggregate capital",
            )
            assert raw_tail_parity.canonical_equal
            assert raw_tail_parity.residual_attributable
            assert raw_tail_parity.raw_residual_rub == -aggregate_raw_tail
            assert Decimal(str(balance_after_identity_resolution[1])) == expected_capital

        repeated_identity_resolution = _process(
            runtime.db_path, "2026-08-15T08:13:40Z"
        )
        assert repeated_identity_resolution["status"] == "caught_up"
        assert repeated_identity_resolution["identity_retry_count"] == 0
        assert repeated_identity_resolution["summary"]["fulfilled"] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9410 "
                "AND event_type='handoff_debit'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone() == balance_after_identity_resolution

            # Return the fixture aggregate to raw equality for later recovery
            # assertions.  The previous debit already proved the canonical
            # gate accepts and preserves the diagnostic-only raw tail.
            exact_detail_capital = sum(
                (
                    Decimal(str(row[0]))
                    for row in conn.execute(
                        "SELECT capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                        "WHERE nm_id=101"
                    ).fetchall()
                ),
                Decimal("0"),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                "SET capital_rub=? WHERE version_id='wf_stage7c' "
                "AND warehouse_key='ff' AND nm_id=101",
                (format(exact_detail_capital, "f"),),
            )
            conn.commit()

        # A frozen acceptance watermark closes independently of rows appended
        # afterwards.  Two bounded rows reach W exactly; the third remains a
        # normal live suffix until the next scheduled-equivalent pass.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn, order_id=9420, supplier="new", wb="waiting",
                observed_at="2026-08-15T08:14:00Z",
            )
            _insert_post_t_order(
                conn, order_id=9421, supplier="new", wb="waiting",
                observed_at="2026-08-15T08:14:01Z",
            )
            frozen_w = int(
                conn.execute(
                    "SELECT MAX(observation_sequence) FROM "
                    "sheet_vitrina_v1_wb_supplies_fbs_status_observations"
                ).fetchone()[0]
            )
            _insert_post_t_order(
                conn, order_id=9422, supplier="new", wb="waiting",
                observed_at="2026-08-15T08:14:02Z",
            )
            post_w = int(
                conn.execute(
                    "SELECT MAX(observation_sequence) FROM "
                    "sheet_vitrina_v1_wb_supplies_fbs_status_observations"
                ).fetchone()[0]
            )
            assert post_w > frozen_w
            conn.commit()
        wb_before_watermark = _wb_evidence_digest(runtime.db_path)
        watermark_closed = _process(
            runtime.db_path, "2026-08-15T08:14:10Z", limit=2
        )
        assert watermark_closed["last_status_observation_sequence"] == frozen_w
        assert watermark_closed["processed_count"] == 2
        assert watermark_closed["pending_count"] == 1
        assert _wb_evidence_digest(runtime.db_path) == wb_before_watermark
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id IN (9420,9421)"
            ).fetchone()[0] == 2
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9422"
            ).fetchone()[0] == 0
        post_w_drained = _process(
            runtime.db_path, "2026-08-15T08:14:20Z", limit=2
        )
        assert post_w_drained["last_status_observation_sequence"] == post_w
        assert post_w_drained["processed_count"] == 1
        assert _process(runtime.db_path, "2026-08-15T08:14:30Z", limit=2)[
            "processed_count"
        ] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id IN (9420,9421,9422)"
            ).fetchone()[0] == 3

        # Exact commit order, rather than business-day bucketing, controls the
        # frozen FBS cost.  The already fulfilled order keeps its old WAC;
        # an overhead commit atomically changes current facility/FBS capital;
        # only the later handoff freezes the resulting WAC.
        with sqlite3.connect(runtime.db_path) as conn:
            before_overhead = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            before_overhead_pool = conn.execute(
                "SELECT nm_id,quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' ORDER BY nm_id"
            ).fetchall()
            old_frozen = Decimal(
                str(
                    conn.execute(
                        f"SELECT frozen_wac_rub FROM {EVENTS_TABLE} "
                        "WHERE order_id=9400 AND event_type='handoff_debit'"
                    ).fetchone()[0]
                )
            )
        overhead_identity = DocumentIdentity(
            request_id="overhead:same-day-cutover:fac-moscow:fbs:101",
            source_system="operator_ui",
            source_type="ff_pool_overhead_manual",
            source_id="synthetic-same-day-cutover",
            source_revision="sha256:" + "8" * 64,
            idempotency_epoch=1,
            actor="warehouse-operator",
            business_date="2026-08-15",
        )
        overhead_preview = service.accept_preview(
            identity=overhead_identity,
            document_kind="pool_overhead",
            manifest={
                "facility_id": "fac_moscow",
                "scope": "FBS",
                "amount_rub": "3.00",
                "category": "storage",
                "comment": "",
                "source_mode": "manual",
            },
        )
        overhead_posted = service.post(str(overhead_preview["request_id"]))
        assert overhead_posted["state"] == "posted"
        assert overhead_posted["publication"]["status"] == "queued"
        repeated_overhead = service.post(str(overhead_preview["request_id"]))
        assert repeated_overhead["document"] == overhead_posted["document"]
        assert repeated_overhead["publication"]["queue_id"] == (
            overhead_posted["publication"]["queue_id"]
        )
        with sqlite3.connect(runtime.db_path) as conn:
            after_overhead = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            after_overhead_pool = conn.execute(
                "SELECT nm_id,quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' ORDER BY nm_id"
            ).fetchall()
            assert int(after_overhead[0]) == int(before_overhead[0])
            assert [tuple(row[:2]) for row in after_overhead_pool] == [
                tuple(row[:2]) for row in before_overhead_pool
            ]
            assert sum(
                (Decimal(str(row[2])) for row in after_overhead_pool),
                Decimal("0"),
            ) - sum(
                (Decimal(str(row[2])) for row in before_overhead_pool),
                Decimal("0"),
            ) == Decimal("3.00")
            with localcontext() as context:
                context.prec = 160
                expected_after_overhead_wac = Decimal(
                    str(after_overhead[1])
                ) / Decimal(int(after_overhead[0]))
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                "WHERE stable_source_id=?",
                (
                    "pool_overhead:"
                    + str(overhead_posted["document"]["document_id"]),
                ),
            ).fetchone()[0] == 1
            _insert_post_t_order(
                conn,
                order_id=9430,
                supplier="complete",
                wb="sorted",
                observed_at="2026-08-15T08:15:00Z",
            )
            conn.commit()
        after_cutover_handoff = _process(
            runtime.db_path, "2026-08-15T08:15:10Z"
        )
        assert after_cutover_handoff["summary"]["fulfilled"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            old_frozen_after = Decimal(
                str(
                    conn.execute(
                        f"SELECT frozen_wac_rub FROM {EVENTS_TABLE} "
                        "WHERE order_id=9400 AND event_type='handoff_debit'"
                    ).fetchone()[0]
                )
            )
            new_event = conn.execute(
                f"SELECT frozen_wac_rub,capital_delta_rub,details_json FROM {EVENTS_TABLE} "
                "WHERE order_id=9430 AND event_type='handoff_debit'"
            ).fetchone()
            assert old_frozen_after == old_frozen
            assert Decimal(str(new_event[0])) == expected_after_overhead_wac
            with localcontext() as context:
                context.prec = 160
                assert Decimal(str(new_event[1])) + expected_after_overhead_wac == 0
            new_cost_basis = json.loads(str(new_event[2]))["cost_basis"]
            assert new_cost_basis["contract"] == "fbs_handoff_current_facility_wac_v1"
            assert int(new_cost_basis["source_operation_rowid"]) > 0
        dependent_storno = service.accept_preview(
            identity=DocumentIdentity(
                request_id="overhead:same-day-cutover:dependent-storno",
                source_system="operator_ui",
                source_type="ff_pool_overhead_storno",
                source_id=str(overhead_posted["document"]["document_id"]),
                source_revision="sha256:" + "9" * 64,
                idempotency_epoch=1,
                actor="warehouse-operator",
                business_date="2026-08-15",
            ),
            document_kind="storno",
            manifest={
                "target_document_id": str(
                    overhead_posted["document"]["document_id"]
                )
            },
        )
        dependent_storno_result = service.post(
            str(dependent_storno["request_id"])
        )
        assert dependent_storno_result["state"] == "blocked"
        assert dependent_storno_result["error"]["code"] == (
            "overhead_storno_dependent_handoff"
        )

        drifted_recovery = service.accept_preview(
            identity=DocumentIdentity(
                request_id="guided:26gn527:drifted-recovery",
                source_system="operator_ui",
                source_type="guided_acceptance_recovery",
                source_id=str(replacement_posted["document"]["document_id"]),
                source_revision="sha256:" + "6" * 64,
                idempotency_epoch=1,
                actor="warehouse-operator",
                business_date="2026-08-15",
            ),
            document_kind="storno",
            manifest={
                "target_document_id": str(replacement_posted["document"]["document_id"])
            },
        )
        drifted_result = service.post(str(drifted_recovery["request_id"]))
        assert drifted_result["state"] == "blocked"
        assert drifted_result["error"]["code"] in {
            "guided_recovery_pool_drift",
            "guided_recovery_aggregate_drift",
            "guided_recovery_dependent_state_drift",
            "guided_recovery_projection_drift",
        }

        # A matched order for a known facility can still reference a SKU that
        # is absent from the immutable cutover manifest.  It is quarantined in
        # the existing identity-pending lane, while the later valid row in the
        # same suffix is processed and the durable cursor advances.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn,
                order_id=9440,
                supplier="complete",
                wb="sorted",
                observed_at="2026-08-15T08:40:00Z",
                source_nm_id=999,
                source_chrt_id=1999,
                seller_sku="seller-999",
                barcode="sku-999",
            )
            _insert_post_t_order(
                conn,
                order_id=9441,
                supplier="new",
                wb="waiting",
                observed_at="2026-08-15T08:40:01Z",
            )
            expected_cursor = int(
                conn.execute(
                    "SELECT MAX(observation_sequence) FROM "
                    "sheet_vitrina_v1_wb_supplies_fbs_status_observations"
                ).fetchone()[0]
            )
            pool_before = conn.execute(
                "SELECT facility_id,pool,nm_id,quantity,capital_rub FROM "
                "sheet_vitrina_v1_ff_pool_balances "
                "ORDER BY facility_id,pool,nm_id"
            ).fetchall()
            aggregate_before = conn.execute(
                "SELECT version_id,warehouse_key,nm_id,quantity,capital_rub FROM "
                "sheet_vitrina_v1_warehouse_functional_balances "
                "ORDER BY version_id,warehouse_key,nm_id"
            ).fetchall()
            conn.commit()

        quarantined_sku = _process(runtime.db_path, "2026-08-15T08:40:10Z")
        assert quarantined_sku["status"] == "caught_up_identity_pending"
        assert quarantined_sku["processed_count"] == 2
        assert quarantined_sku["pending_count"] == 0
        assert quarantined_sku["identity_pending_count"] == 1
        assert quarantined_sku["summary"]["identity_pending"] == 1
        assert quarantined_sku["summary"]["reserved"] == 1
        assert quarantined_sku["last_status_observation_sequence"] == expected_cursor
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT reason_code,reason_detail_code FROM {IDENTITY_PENDING_TABLE} "
                "WHERE order_id=9440"
            ).fetchone() == (
                "identity_evidence_missing_or_drifted",
                "order_sku_unmapped",
            )
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9440"
            ).fetchone()[0] == 0
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9441 "
                "AND event_type='reserve'"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {RECONCILIATION_TABLE} WHERE order_id=9440"
            ).fetchone()[0] == 0
            unresolved_reason = conn.execute(
                _current_cte(lifecycle_available=True)
                + " SELECT cost_status,cost_reason,lifecycle_reason "
                "FROM current_order WHERE order_id=9440"
            ).fetchone()
            assert unresolved_reason == (
                "cost_unresolved",
                "sku_mapping_missing_or_ambiguous",
                "order_sku_unmapped",
            )
            assert int(
                conn.execute(
                    f"SELECT last_status_observation_sequence FROM {DRAIN_STATE_TABLE}"
                ).fetchone()[0]
            ) == expected_cursor
            assert conn.execute(
                "SELECT facility_id,pool,nm_id,quantity,capital_rub FROM "
                "sheet_vitrina_v1_ff_pool_balances "
                "ORDER BY facility_id,pool,nm_id"
            ).fetchall() == pool_before
            assert conn.execute(
                "SELECT version_id,warehouse_key,nm_id,quantity,capital_rub FROM "
                "sheet_vitrina_v1_warehouse_functional_balances "
                "ORDER BY version_id,warehouse_key,nm_id"
            ).fetchall() == aggregate_before

        repeated_quarantine = _process(runtime.db_path, "2026-08-15T08:40:20Z")
        assert repeated_quarantine["processed_count"] == 0
        assert repeated_quarantine["pending_count"] == 0
        assert repeated_quarantine["identity_retry_count"] == 1
        assert repeated_quarantine["identity_pending_count"] == 1
        assert repeated_quarantine["summary"]["identity_pending"] == 1
        assert repeated_quarantine["last_status_observation_sequence"] == expected_cursor
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} WHERE order_id=9440"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id IN (9440,9441)"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT facility_id,pool,nm_id,quantity,capital_rub FROM "
                "sheet_vitrina_v1_ff_pool_balances "
                "ORDER BY facility_id,pool,nm_id"
            ).fetchall() == pool_before
            assert conn.execute(
                "SELECT version_id,warehouse_key,nm_id,quantity,capital_rub FROM "
                "sheet_vitrina_v1_warehouse_functional_balances "
                "ORDER BY version_id,warehouse_key,nm_id"
            ).fetchall() == aggregate_before
    print("ff_pool_fbs_lifecycle_smoke: OK")
    return 0


def _insert_post_t_order(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    supplier: str,
    wb: str,
    source_created_at: str = "2026-08-14T06:00:00Z",
    observed_at: str = "2026-08-14T06:01:00Z",
    quantity: int = 1,
    identity_outcome: str = "matched",
    source_nm_id: int = 101,
    source_chrt_id: int = 201,
    seller_sku: str = "seller-101",
    barcode: str = "sku-101",
) -> None:
    revision = f"post_revision_{order_id}_v1"
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
               observation_id,order_id,source_revision,supply_id,delivery_type,
               source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
               skus_json,observed_at,collector_date_from,collector_date_to,collector_cursor
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"post_observation_{order_id}", order_id, revision, "post-supply", "fbs",
            source_created_at, 501, 601, source_nm_id, source_chrt_id,
            seller_sku, json.dumps([barcode]),
            observed_at, 1, 2, 0,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
               evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
               barcode,seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,
               evidence_digest,observed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"post_identity_evidence_{order_id}", order_id, revision, 501,
            source_nm_id, source_chrt_id, barcode, seller_sku, identity_outcome,
            "warehouse_mapping_1",
            "identity_mapping_1" if identity_outcome == "matched" else "",
            "sha256:" + hashlib.sha256(f"identity:{order_id}".encode()).hexdigest(),
            observed_at,
        ),
    )
    _append_status(
        conn,
        order_id=order_id,
        revision=revision,
        supplier_status=supplier,
        wb_status=wb,
        episode=1,
        observed_at=observed_at,
        insert_current=True,
        quantity=quantity,
    )


class _DocClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def _append_status(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    revision: str,
    supplier_status: str,
    wb_status: str,
    episode: int,
    observed_at: str,
    insert_current: bool,
    quantity: int = 1,
) -> None:
    existing_order = conn.execute(
        """SELECT 1 FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
           WHERE order_id=? AND source_revision=?""",
        (order_id, revision),
    ).fetchone()
    if existing_order is None:
        prior = conn.execute(
            """SELECT supply_id,delivery_type,source_created_at,warehouse_id,office_id,
                      nm_id,chrt_id,seller_sku,skus_json,collector_date_from,
                      collector_date_to,collector_cursor
               FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
               WHERE order_id=? ORDER BY observation_sequence DESC LIMIT 1""",
            (order_id,),
        ).fetchone()
        if prior is None:
            raise AssertionError(f"missing order source for status revision {order_id}/{revision}")
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
                   observation_id,order_id,source_revision,supply_id,delivery_type,
                   source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
                   skus_json,observed_at,collector_date_from,collector_date_to,
                   collector_cursor
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"status_order_{order_id}_{episode}", order_id, revision,
                *tuple(prior[:9]), observed_at, *tuple(prior[9:]),
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
                   evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                   barcode,seller_sku,outcome,warehouse_mapping_id,
                   identity_mapping_id,evidence_digest,observed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"status_identity_{order_id}_{episode}", order_id, revision,
                int(prior[3]), int(prior[5]), int(prior[6]), "sku-101",
                str(prior[7]), "matched", "warehouse_mapping_1",
                "identity_mapping_1",
                "sha256:" + hashlib.sha256(
                    f"status-identity:{order_id}:{revision}".encode()
                ).hexdigest(),
                observed_at,
            ),
        )
    digest = "sha256:" + hashlib.sha256(
        f"{order_id}:{revision}:{supplier_status}:{wb_status}:{quantity}".encode("utf-8")
    ).hexdigest()
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
               observation_id,order_id,order_revision,status_digest,supplier_status,
               wb_status,positive_quantity,observed_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            f"status_{order_id}_{episode}", order_id, revision, digest,
            supplier_status, wb_status, quantity, observed_at,
        ),
    )
    if insert_current:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_current(
                   order_id,order_revision,status_digest,supplier_status,wb_status,
                   source_observed_at,local_first_seen_at,local_last_seen_at,
                   observation_count,episode_sequence
               ) VALUES(?,?,?,?,?,'',?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                   order_revision=excluded.order_revision,
                   status_digest=excluded.status_digest,
                   supplier_status=excluded.supplier_status,
                   wb_status=excluded.wb_status,
                   local_last_seen_at=excluded.local_last_seen_at,
                   observation_count=excluded.observation_count,
                   episode_sequence=excluded.episode_sequence""",
            (
                order_id, revision, digest, supplier_status, wb_status,
                observed_at, observed_at, episode, episode,
            ),
        )


def _process(path: Path, timestamp: str, *, limit: int = 500) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        result = process_post_t_fbs_lifecycle(
            conn, occurred_at=timestamp, limit=limit, schema_ready=True
        )
        conn.commit()
        return result


def _cutover_id(conn: sqlite3.Connection) -> str:
    return str(
        conn.execute(
            "SELECT cutover_id FROM sheet_vitrina_v1_ff_pool_cutover_manifests"
        ).fetchone()[0]
    )


def _wb_evidence_digest(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        payload = {
            "orders": conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations ORDER BY observation_sequence"
            ).fetchall(),
            "statuses": conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_status_observations ORDER BY observation_sequence"
            ).fetchall(),
            "current": conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_status_current ORDER BY order_id"
            ).fetchall(),
        }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
