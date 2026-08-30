#!/usr/bin/env python3
"""Business-time product-capital outbox/projection contract smoke."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.own_product_capital import (  # noqa: E402
    OwnProductCapitalBlock,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_AVG_COST_RUB_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.application.warehouse_business_projection import (  # noqa: E402
    CURRENT_ROW_TABLE,
    OUTBOX_TABLE,
    STATE_TABLE,
    apply_warehouse_business_projection_overlay,
    drain_warehouse_business_projection_outbox,
    ensure_functional_version_business_time_schema,
    load_warehouse_business_projection_status,
    publish_functional_version_business_projection,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaWriteTarget,
)
from packages.application.warehouse_functional import (  # noqa: E402
    enqueue_warehouse_targeted_recalculation,
    ensure_warehouse_functional_schema,
)
from packages.business_time import business_date_from_timestamp  # noqa: E402


NOW = "2026-07-25T10:00:00Z"
LINES = [
    {
        "line_id": "line-101",
        "nm_id": 101,
        "qty": "10",
        "unit_price": "10",
        "invoice_value_cny": "100",
    }
]


def main() -> None:
    assert business_date_from_timestamp("2026-07-20T19:30:00Z") == "2026-07-21"
    with tempfile.TemporaryDirectory(
        prefix="warehouse-business-projection-"
    ) as temp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp) / "runtime"
        )
        block = OwnProductCapitalBlock(
            runtime=runtime,
            timestamp_factory=lambda: NOW,
        )
        block.record_supplier_payment(
            payment_id="payment-same-day",
            shipment_id="shipment-same-day",
            effective_date="2026-07-21",
            invoice_total_cny="100",
            paid_cny="100",
            paid_rub="1000",
            product_lines=LINES,
            actual_shipment_date="2026-07-21",
            expenses_complete=True,
            recalculate=False,
        )
        receipt = block.record_ff_receipt(
            movement_id="ff-same-day",
            shipment_id="shipment-same-day",
            effective_date="2026-07-21",
            quantities_by_nm={101: "10"},
            expenses_complete=True,
        )
        assert receipt["lines"][0]["quantity"] == "10", receipt
        rebuild = block.recalculate(
            date_from="2026-07-21",
            date_to="2026-07-25",
        )
        assert rebuild.date_count == 5, rebuild

        first_status = load_warehouse_business_projection_status(runtime)
        assert first_status["revision_no"] == 0, first_status
        assert first_status["outbox_counts"] == {
            "pending_exact_functional": 2
        }, first_status
        assert _current_metrics(runtime) == {}

        first_publication = _publish_exact_version(
            runtime,
            version_id="functional-v1",
            capital_rub="1000",
            source_revision="sha256:functional-v1",
        )
        assert first_publication["status"] == "success", first_publication
        assert first_publication["resolved_replay_signal_count"] == 0
        first_status = load_warehouse_business_projection_status(runtime)
        assert first_status["revision_no"] == 1, first_status
        assert first_status["outbox_counts"] == {
            "pending_exact_functional": 2
        }, first_status
        assert first_status["health_status"] == "pending_exact_functional"
        assert first_status["reconciliation"]["status"] == "published_exact"
        with sqlite3.connect(runtime.db_path) as conn:
            row = conn.execute(
                f"SELECT metrics_json,provenance_json FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date='2026-07-25' AND nm_id=101"
            ).fetchone()
            stale_metrics = json.loads(str(row[0]))
            stale_metrics[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] = 999999.0
            stale_provenance = json.loads(str(row[1]))
            stale_provenance["source"] = "canonical_own_capital_events"
            conn.execute(
                f"UPDATE {CURRENT_ROW_TABLE} SET metrics_json=?,provenance_json=? "
                "WHERE as_of_date='2026-07-25' AND nm_id=101",
                (
                    json.dumps(stale_metrics, ensure_ascii=False, sort_keys=True),
                    json.dumps(stale_provenance, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
        guarded = block.load_daily_metric_lookup(
            "2026-07-25",
            requested_nm_ids=[101],
        )
        assert guarded[101][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] == 1000.0, guarded
        guarded_snapshot = apply_warehouse_business_projection_overlay(
            runtime,
            snapshot=SheetVitrinaV1Envelope(
                plan_version="smoke",
                snapshot_id="guarded-overlay",
                as_of_date="2026-07-25",
                date_columns=["2026-07-25"],
                temporal_slots=[],
                source_temporal_policies={},
                sheets=[
                    SheetVitrinaWriteTarget(
                        sheet_name="DATA_VITRINA",
                        write_start_cell="A1",
                        write_rect="A1:C2",
                        clear_range="A:C",
                        write_mode="replace",
                        partial_update_allowed=False,
                        header=["label", "row_id", "2026-07-25"],
                        rows=[["", "SKU:101|own_total_product_capital_rub", 1234.0]],
                        row_count=1,
                        column_count=3,
                    )
                ],
                metadata={
                    "warehouse_history_coverage": {
                        "2026-07-25": {
                            "functional_version_id": "functional-v1"
                        }
                    }
                },
            ),
        )
        assert guarded_snapshot.sheets[0].rows[0][2] == 1234.0
        overlay_evidence = guarded_snapshot.metadata["warehouse_business_projection"]
        assert overlay_evidence["ignored_unbound_row_count"] == 1
        assert overlay_evidence["incidents"][0]["status"] == "historical_repair_required"
        guarded_status = load_warehouse_business_projection_status(runtime)
        assert guarded_status["health_status"] == "historical_repair_required"
        assert guarded_status["reconciliation"]["unbound_row_count"] == 1
        before_qty = _all_quantity_digest(runtime)

        cost = block.record_order_level_cost_payment(
            document_id="bank-fee-21",
            shipment_id="shipment-same-day",
            effective_date="2026-07-21",
            capital_rub="100",
            product_lines=LINES,
            component="bank_fee",
            actual_shipment_date="2026-07-21",
            actual_ff_acceptance_date="2026-07-21",
            expenses_complete=True,
            provenance={
                "source_sha256": sha256(b"bank-fee-21").hexdigest(),
                "business_date": "2026-07-21",
            },
        )
        assert not cost["idempotent"], cost
        block.recalculate(date_from="2026-07-21", date_to="2026-07-25")
        pending_cost = _current_metrics(runtime)
        assert _all_quantity_digest(runtime) == before_qty
        assert pending_cost["2026-07-25"][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] == 999999.0
        second_status = load_warehouse_business_projection_status(runtime)
        assert second_status["revision_no"] == 1, second_status
        assert second_status["outbox_counts"].get("pending_exact_functional")

        second_publication = _publish_exact_version(
            runtime,
            version_id="functional-v2",
            capital_rub="1100",
            source_revision="sha256:functional-v2",
        )
        assert second_publication["status"] == "success", second_publication
        after_cost = _current_metrics(runtime)
        assert _all_quantity_digest(runtime) == before_qty
        assert after_cost["2026-07-25"][OWN_TOTAL_QTY_METRIC_KEY] == 10.0
        assert after_cost["2026-07-25"][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] == 1100.0
        assert after_cost["2026-07-25"][OWN_AVG_COST_RUB_METRIC_KEY] == 110.0

        repeated = block.record_order_level_cost_payment(
            document_id="bank-fee-21",
            shipment_id="shipment-same-day",
            effective_date="2026-07-21",
            capital_rub="100",
            product_lines=LINES,
            component="bank_fee",
            actual_shipment_date="2026-07-21",
            actual_ff_acceptance_date="2026-07-21",
            expenses_complete=True,
            provenance={
                "source_sha256": sha256(b"bank-fee-21").hexdigest(),
                "business_date": "2026-07-21",
            },
        )
        assert repeated["idempotent"], repeated
        block.recalculate(date_from="2026-07-21", date_to="2026-07-25")
        repeated_status = load_warehouse_business_projection_status(runtime)
        assert repeated_status["revision_no"] == 2, repeated_status
        assert repeated_status["outbox_counts"] == {
            "pending_exact_functional": 3
        }, repeated_status
    print("warehouse_business_projection_smoke: OK")


def _publish_exact_version(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    version_id: str,
    capital_rub: str,
    source_revision: str,
) -> dict:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        ensure_functional_version_business_time_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,
                created_at,business_effective_date,published_at
            ) VALUES(?, 'warehouse_functional_cutover_v1','hourly_wb_sync',?,
                     'good',?,?, '{}',?,'2026-07-25',?)
            """,
            (
                version_id,
                NOW,
                "sha256:plan:" + version_id,
                "sha256:local:" + version_id,
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                snapshot_id,version_id,fetched_at,snapshot_date,
                requested_nm_ids_json,pagination_complete,page_count,
                page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,
                items_json,created_at
            ) VALUES(?,?,?,'2026-07-25','[101]',1,1,'[0]',1,?, '[]','[]',?)
            """,
            (
                "snapshot:" + version_id,
                version_id,
                NOW,
                "sha256:snapshot:" + version_id,
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES(?, 'ff',101,'10',?,?,'10','moving_weighted_average',
                     1,'0','0','0','{}')
            """,
            (version_id, str(float(capital_rub) / 10), capital_rub),
        )
        result = publish_functional_version_business_projection(
            conn,
            published_version_id=version_id,
            business_effective_date="2026-07-25",
            published_at=NOW,
            source_revision=source_revision,
        )
        conn.commit()
    return result


def _assert_same_day_receipt(runtime: RegistryUploadDbBackedRuntime) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        receipt_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_own_capital_events
            WHERE event_id='stage_transfer:ff-same-day:101'
              AND stage_from='PRODUCTION_TO_FF' AND stage_to='FF'
            """
        ).fetchone()[0]
        assert receipt_count == 1
    metrics = _current_metrics(runtime)
    assert sorted(metrics) == [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-25",
    ]
    for as_of_date, row in metrics.items():
        assert row[own_stage_metric_key("PRODUCTION_TO_FF", "qty")] == 0.0
        assert row[own_stage_metric_key("FF", "qty")] == 10.0
        assert row[OWN_TOTAL_QTY_METRIC_KEY] == 10.0
        assert row[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] == 1000.0


def _current_metrics(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, dict]:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT as_of_date,metrics_json
            FROM {CURRENT_ROW_TABLE}
            WHERE nm_id=101
            ORDER BY as_of_date
            """
        ).fetchall()
    return {
        str(row["as_of_date"]): json.loads(str(row["metrics_json"]))
        for row in rows
    }


def _all_quantity_digest(runtime: RegistryUploadDbBackedRuntime) -> str:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT as_of_date,nm_id,metrics_json
            FROM {CURRENT_ROW_TABLE}
            ORDER BY as_of_date,nm_id
            """
        ).fetchall()
    quantities = {
        f"{row['as_of_date']}|{row['nm_id']}": {
            key: value
            for key, value in sorted(
                json.loads(str(row["metrics_json"])).items()
            )
            if key.endswith("_qty") or key.endswith("_qty_total")
        }
        for row in rows
    }
    return json.dumps(
        quantities,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _assert_cost_only_preserves_legacy_total_quantities(
    runtime: RegistryUploadDbBackedRuntime,
    block: OwnProductCapitalBlock,
) -> None:
    as_of_date = "2026-07-21"
    quantity_key = own_stage_total_metric_key("FF", "qty")
    unit_cost_key = own_stage_total_metric_key("FF", "unit_cost_rub")
    with sqlite3.connect(runtime.db_path) as conn:
        row = conn.execute(
            f"SELECT metrics_json FROM {CURRENT_ROW_TABLE} WHERE as_of_date=? AND nm_id=0",
            (as_of_date,),
        ).fetchone()
        assert row is not None
        metrics = json.loads(str(row[0]))
        metrics[quantity_key] = 11.0
        metrics[OWN_TOTAL_QTY_TOTAL_METRIC_KEY] = 11.0
        conn.execute(
            f"UPDATE {CURRENT_ROW_TABLE} SET metrics_json=? WHERE as_of_date=? AND nm_id=0",
            (
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                as_of_date,
            ),
        )
        conn.commit()
    quantity_before = _all_quantity_digest(runtime)
    block.record_order_level_cost_payment(
        document_id="bank-fee-total-drift",
        shipment_id="shipment-same-day",
        effective_date=as_of_date,
        capital_rub="110",
        product_lines=LINES,
        component="bank_fee",
        actual_shipment_date=as_of_date,
        actual_ff_acceptance_date=as_of_date,
        expenses_complete=True,
        provenance={
            "source_sha256": sha256(b"bank-fee-total-drift").hexdigest(),
            "business_date": as_of_date,
        },
    )
    block.recalculate(date_from=as_of_date, date_to="2026-07-25")
    assert _all_quantity_digest(runtime) == quantity_before
    with sqlite3.connect(runtime.db_path) as conn:
        row = conn.execute(
            f"SELECT metrics_json FROM {CURRENT_ROW_TABLE} WHERE as_of_date=? AND nm_id=0",
            (as_of_date,),
        ).fetchone()
    metrics = json.loads(str(row[0]))
    assert metrics[quantity_key] == 11.0, metrics
    assert metrics[OWN_TOTAL_QTY_TOTAL_METRIC_KEY] == 11.0, metrics
    assert metrics[unit_cost_key] == metrics["total_own_capital_FF_capital_rub"] / 11.0


def _insert_request(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    suffix: str,
    business_date: str,
) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
            ) VALUES(?,?,?,?,?,'bank_fee_cost','queued',?,NULL,NULL,NULL)
            """,
            (
                "smoke-" + suffix,
                "bank_fee:" + suffix,
                "revision:" + suffix,
                business_date,
                "[101]",
                NOW,
            ),
        )
        conn.commit()


def _projection_digest(runtime: RegistryUploadDbBackedRuntime) -> tuple:
    with sqlite3.connect(runtime.db_path) as conn:
        state = conn.execute(
            f"SELECT revision_no,revision_id FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT as_of_date,nm_id,revision_id,metrics_json,row_fingerprint
            FROM {CURRENT_ROW_TABLE}
            ORDER BY as_of_date,nm_id
            """
        ).fetchall()
    return state, rows


def _assert_failure_keeps_last_good(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    _insert_request(runtime, suffix="failure", business_date="2026-07-22")
    before = _projection_digest(runtime)

    def inject(phase: str) -> None:
        if phase == "business_projection_before_switch":
            raise RuntimeError("injected candidate failure")

    try:
        drain_warehouse_business_projection_outbox(
            runtime,
            published_at=NOW,
            inject_failure=inject,
        )
    except RuntimeError as exc:
        assert "injected candidate failure" in str(exc)
    else:
        raise AssertionError("failure injection did not abort")
    assert _projection_digest(runtime) == before
    status = load_warehouse_business_projection_status(runtime)
    assert status["outbox_counts"].get("error") == 1, status
    assert (
        status.get("latest_failure", {}).get("error")
        == "injected candidate failure"
    ), status
    recovered = drain_warehouse_business_projection_outbox(
        runtime,
        published_at=NOW,
    )
    assert recovered["status"] == "success", recovered


def _assert_concurrent_drain_is_exactly_once(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    _insert_request(runtime, suffix="concurrent", business_date="2026-07-23")
    barrier = threading.Barrier(2)
    results: list[dict] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=2)
            results.append(
                drain_warehouse_business_projection_outbox(
                    runtime,
                    published_at=NOW,
                )
            )
        except BaseException as exc:  # surfaced by the parent assertion
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures, failures
    assert sorted(item["status"] for item in results) == [
        "no_op",
        "success",
    ], results
    with sqlite3.connect(runtime.db_path) as conn:
        count = conn.execute(
            f"""
            SELECT COUNT(*) FROM {OUTBOX_TABLE}
            WHERE request_id='smoke-concurrent' AND status='complete'
            """
        ).fetchone()[0]
    assert count == 1


def _assert_partial_functional_source_keeps_quantities(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    quantity_before = _all_quantity_digest(runtime)
    metrics_before = _current_metrics(runtime)
    revision_before = load_warehouse_business_projection_status(runtime)[
        "revision_no"
    ]
    queued = enqueue_warehouse_targeted_recalculation(
        runtime=runtime,
        stable_source_id="supplier_costs:partial-smoke",
        source_revision="sha256:partial-functional-source",
        effective_date="2026-07-24",
        affected_nm_ids=[101],
        requested_at=NOW,
    )
    publication = dict(queued.get("business_projection") or {})
    assert publication.get("status") == "success", queued
    assert publication.get("idempotent") is True, queued
    assert publication.get("diagnostics", {}).get("last_good_preserved") is True
    assert (
        publication.get("diagnostics", {}).get(
            "awaiting_exact_functional_replay"
        )
        is True
    )
    after = _current_metrics(runtime)
    assert _all_quantity_digest(runtime) == quantity_before
    assert after == metrics_before
    status = load_warehouse_business_projection_status(runtime)
    assert not status["updating"], status
    assert status["queue_counts"].get("queued") == 1, status
    assert status["revision_no"] == revision_before, status


def _assert_late_transit_cost_scope_is_bounded(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    quantity_before = _all_quantity_digest(runtime)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
            ) VALUES(?,?,?,?,?,'functional_source_revision','error',?,NULL,?,?)
            """,
            (
                "smoke-late-transit-cost",
                "functional_queue:wb_transit_cost:40422317",
                "sha256:late-transit-cost",
                "2025-02-21",
                "[101]",
                NOW,
                NOW,
                "bounded business projection exceeds 366 dates",
            ),
        )
        conn.commit()
    result = drain_warehouse_business_projection_outbox(
        runtime,
        published_at=NOW,
    )
    diagnostics = dict(result.get("diagnostics") or {})
    assert result.get("status") == "success", result
    assert diagnostics.get("cost_only") is True, result
    assert diagnostics.get("scope_truncated") is True, result
    assert (
        diagnostics.get("scope_truncation_reason")
        == "late_cost_outside_active_bounded_business_projection"
    ), result
    assert diagnostics.get("requested_business_effective_date") == "2025-02-21"
    assert diagnostics.get("applied_business_effective_date") == "2026-07-21"
    assert result.get("affected_dates") == [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-25",
    ], result
    assert _all_quantity_digest(runtime) == quantity_before
    with sqlite3.connect(runtime.db_path) as conn:
        source_kind, status = conn.execute(
            f"SELECT source_kind,status FROM {OUTBOX_TABLE} WHERE request_id=?",
            ("smoke-late-transit-cost",),
        ).fetchone()
    assert source_kind == "functional_transit_cost_revision"
    assert status == "complete"


if __name__ == "__main__":
    main()
