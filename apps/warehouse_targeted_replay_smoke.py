#!/usr/bin/env python3
"""Targeted replay atomicity, scope, lock and I/O contract smoke."""

from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.own_product_capital import OwnProductCapitalBlock  # noqa: E402
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_TOTAL_QTY_METRIC_KEY,
    own_stage_metric_key,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
)
from packages.application.warehouse_functional import (  # noqa: E402
    FUNCTIONAL_CUTOVER_ID,
    ensure_warehouse_functional_schema,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    WarehouseFunctionalBusyError,
    warehouse_functional_write_lock,
)
from packages.application.warehouse_business_projection import (  # noqa: E402
    CURRENT_ROW_TABLE,
    ensure_functional_version_business_time_schema,
)
from packages.application.warehouse_targeted_replay import (  # noqa: E402
    WarehouseTargetedSupplierReplay,
)


SHIPMENT_ID = "targeted-shipment"
NOW = "2026-07-25T10:00:00Z"
CUTOVER_AT = "2026-07-19T00:00:00Z"


def _allocation(date_value: str = "2026-07-21") -> dict:
    return {
        "shipment_id": SHIPMENT_ID,
        "invoice_no": "TARGET-1",
        "invoice_date": "2026-07-17",
        "first_payment_date": "2026-07-18",
        "actual_shipment_date": date_value,
        "stage": "china_to_ff" if date_value else "production",
        "expenses_complete": False,
        "source_fingerprint": "sha256:target-source-" + (date_value or "production"),
        "calculation_fingerprint": "sha256:target-calculation",
        "blockers": [],
        "lines": [
            {
                "line_id": "line-1",
                "nm_id": 101,
                "quantity": "10",
                "capital_rub": "100",
                "components": [],
            }
        ],
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="warehouse-targeted-replay-") as temp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp) / "runtime")
        runtime.save_supplier_shipment(
            header={
                "shipment_id": SHIPMENT_ID,
                "created_at": NOW,
                "updated_at": NOW,
                "shipment_date": "2026-07-17",
                "order_status": "production",
                "invoice_no": "TARGET-1",
                "invoice_date": "2026-07-17",
            },
            lines=[
                {
                    "line_id": "line-1",
                    "shipment_id": SHIPMENT_ID,
                    "line_type": "product",
                    "sort_order": 1,
                    "internal_nm_id": 101,
                    "qty": 10,
                    "unit_price": 1,
                    "amount": 10,
                    "match_status": "matched",
                }
            ],
        )
        _seed_functional(runtime)
        before_business_time = OwnProductCapitalBlock(
            runtime=runtime,
            timestamp_factory=lambda: NOW,
        ).load_daily_metric_lookup("2026-07-21")
        assert (
            before_business_time[101][own_stage_metric_key("PRODUCTION", "qty")]
            == 12.0
        ), before_business_time
        block = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: NOW,
        )
        trace: list[str] = []
        with sqlite3.connect(runtime.db_path) as trace_conn:
            trace_conn.set_trace_callback(trace.append)
            trace_conn.execute(
                "CREATE TABLE wb_finance_weekly_raw_rows(payload BLOB)"
            )
            trace_conn.executemany(
                "INSERT INTO wb_finance_weekly_raw_rows(payload) VALUES(zeroblob(128))",
                [() for _ in range(5000)],
            )
            trace_conn.commit()
        trace.clear()
        with patch(
            "packages.application.warehouse_targeted_replay.load_supplier_line_cost_breakdown",
            side_effect=lambda **kwargs: _allocation(
                str(kwargs.get("actual_shipment_date_override") or "")
            ),
        ), patch(
            "packages.application.warehouse_targeted_replay._connect_readonly",
            side_effect=lambda path: _traced_connect(path, trace),
        ):
            dry = block.dry_run(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-07-21",
                actor="smoke",
                expected_old_value="",
            )
            assert dry["scope"]["affected_nm_ids"] == [101]
            assert dry["performance"]["copy_bytes"] == 0
            assert not dry["performance"]["full_database_copy"]
            assert dry["performance"]["finance_raw_rows_read"] == 0
            try:
                block.apply(
                    shipment_id=SHIPMENT_ID,
                    new_actual_shipment_date="2026-07-21",
                    actor="smoke",
                    fingerprint="sha256:stale-plan",
                    backup_dir=Path(temp) / "unused",
                    expected_old_value="",
                )
            except ValueError as exc:
                assert "exact current targeted dry-run fingerprint" in str(exc)
            else:
                raise AssertionError("stale targeted plan did not fail closed")
            assert not runtime.load_supplier_shipment(SHIPMENT_ID)["header"].get(
                "actual_shipment_date"
            )
            applied = block.apply(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-07-21",
                actor="smoke",
                fingerprint=dry["fingerprint"],
                backup_dir=Path(temp) / "unused",
                expected_old_value="",
            )
        assert applied["applied"] is True
        _assert_business_time_projection(runtime, applied)
        finance_access = [
            sql
            for sql in trace
            if any(
                marker in sql.casefold()
                for marker in (
                    "from wb_finance_weekly_raw_rows",
                    "join wb_finance_weekly_raw_rows",
                    "update wb_finance_weekly_raw_rows",
                    "into wb_finance_weekly_raw_rows",
                    "delete from wb_finance_weekly_raw_rows",
                )
            )
        ]
        assert finance_access == [], finance_access
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            header = conn.execute(
                "SELECT actual_shipment_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
                (SHIPMENT_ID,),
            ).fetchone()
            active = conn.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()
            china = conn.execute(
                """
                SELECT quantity FROM sheet_vitrina_v1_warehouse_functional_balances
                WHERE version_id=? AND warehouse_key='china_to_ff' AND nm_id=101
                """,
                (active["version_id"],),
            ).fetchone()
            non_target = conn.execute(
                """
                SELECT quantity FROM sheet_vitrina_v1_warehouse_functional_balances
                WHERE version_id=? AND warehouse_key='ff' AND nm_id=202
                """,
                (active["version_id"],),
            ).fetchone()
            active_snapshot = conn.execute(
                """
                SELECT raw_rows_digest FROM sheet_vitrina_v1_warehouse_wb_snapshots
                WHERE version_id=?
                """,
                (active["version_id"],),
            ).fetchone()
            active_documents = conn.execute(
                """
                SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_functional_documents
                WHERE version_id=?
                """,
                (active["version_id"],),
            ).fetchone()[0]
            assert header["actual_shipment_date"] == "2026-07-21"
            assert china["quantity"] == "10"
            assert non_target["quantity"] == "5"
            assert active_snapshot["raw_rows_digest"] == "sha256:wb-snapshot"
            assert active_documents == 3

        with patch(
            "packages.application.warehouse_targeted_replay.load_supplier_line_cost_breakdown",
            side_effect=lambda **kwargs: _allocation(
                str(kwargs.get("actual_shipment_date_override") or "")
            ),
        ):
            second = block.dry_run(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-07-21",
                actor="smoke",
                expected_old_value="2026-07-21",
            )
        assert second["would_change"] is False, {
            "before": second["target_rows_before"],
            "after": second["target_rows_after"],
            "old_date": second["old_actual_shipment_date"],
            "new_date": second["new_actual_shipment_date"],
        }
        with sqlite3.connect(runtime.db_path) as conn:
            manifest_digest = conn.execute(
                """
                SELECT manifest_digest
                FROM sheet_vitrina_v1_warehouse_targeted_undo_manifests
                WHERE publication_id=?
                """,
                (applied["publication_id"],),
            ).fetchone()[0]
        replay = WarehouseTargetedSupplierReplay(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-25T10:00:01Z",
        )
        rollback = replay.rollback(manifest_digest=manifest_digest)
        assert rollback["rolled_back"] is True
        assert not runtime.load_supplier_shipment(SHIPMENT_ID)["header"][
            "actual_shipment_date"
        ]
        assert replay.rollback(manifest_digest=manifest_digest)["idempotent"]
        _failure_is_atomic(runtime)
        _lock_is_shared(runtime)
    print("warehouse_targeted_replay_smoke: OK")


def _traced_connect(path: Path, trace: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(path).resolve()}?mode=ro",
        uri=True,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.set_trace_callback(trace.append)
    return conn


def _seed_functional(runtime: RegistryUploadDbBackedRuntime) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        ensure_functional_version_business_time_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                cutover_id,cutover_at,status,plan_fingerprint,
                source_watermarks_json,absorbed_supply_revisions_json,
                backup_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                FUNCTIONAL_CUTOVER_ID,
                CUTOVER_AT,
                "posted",
                "sha256:cutover",
                "{}",
                "{}",
                "{}",
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,
                business_effective_date,published_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,created_at
            ) VALUES(
                'base',?,'hourly_wb_sync',?,'2026-07-25',?,'good',
                'sha256:base','sha256:local','{}',?
            )
            """,
            (FUNCTIONAL_CUTOVER_ID, NOW, NOW, NOW),
        )
        source = {
            "source_records": [
                {
                    "shipment_id": SHIPMENT_ID,
                    "supplier_flow_id": "supplier_flow_target",
                    "flow_quantity": "10",
                    "flow_capital_rub": "100",
                    "quality": "confirmed_payments_provisional_expenses",
                    "expenses_complete_certification": False,
                },
                {
                    "shipment_id": "unrelated-shipment-same-sku",
                    "supplier_flow_id": "supplier_flow_unrelated",
                    "flow_quantity": "2",
                    "flow_capital_rub": "20",
                    "quality": "confirmed_payments_provisional_expenses",
                    "expenses_complete_certification": False,
                }
            ]
        }
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES('base','production',101,'12','10','120','12',?,0,'0','0','0',?)
            """,
            ("confirmed_payments_provisional_expenses", _json(source)),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES('base','ff',202,'5','20','100','5','moving_weighted_average',0,'0','0','0','{}')
            """
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_states(
                version_id,shipment_id,source_fingerprint,calculation_fingerprint,
                expenses_complete,calculation_available,created_at
            ) VALUES('base',?,'sha256:old','sha256:old',0,1,?)
            """,
            (SHIPMENT_ID, NOW),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                snapshot_id,version_id,fetched_at,snapshot_date,
                requested_nm_ids_json,pagination_complete,page_count,
                page_offsets_json,raw_row_count,raw_rows_digest,
                raw_rows_json,items_json,created_at
            ) VALUES(
                'snapshot-base','base',?,'2026-07-25','[101]',1,1,
                '[0]',1,'sha256:wb-snapshot','[{"nmID":101}]',
                '[{"nm_id":101}]',?
            )
            """,
            (NOW, NOW),
        )
        for historical_date in (
            "2026-07-20",
            "2026-07-21",
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
        ):
            version_id = "daily-" + historical_date
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                    version_id,cutover_id,version_kind,effective_at,
                    business_effective_date,published_at,status,
                    plan_fingerprint,local_source_digest,
                    source_watermarks_json,created_at
                ) VALUES(?,?,'hourly_wb_sync',?,?,?,?,?,?,'{}',?)
                """,
                (
                    version_id,
                    FUNCTIONAL_CUTOVER_ID,
                    # All technical timestamps deliberately point to 25 July.
                    NOW,
                    historical_date,
                    NOW,
                    "good",
                    "sha256:" + version_id,
                    "sha256:local-" + historical_date,
                    NOW,
                ),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                    version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                    cost_covered_quantity,quality,certified,wb_quantity,
                    wb_in_way_to_client,wb_in_way_from_client,provenance_json
                )
                SELECT ?,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                       cost_covered_quantity,quality,certified,wb_quantity,
                       wb_in_way_to_client,wb_in_way_from_client,provenance_json
                FROM sheet_vitrina_v1_warehouse_functional_balances
                WHERE version_id='base'
                """,
                (version_id,),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                    snapshot_id,version_id,fetched_at,snapshot_date,
                    requested_nm_ids_json,pagination_complete,page_count,
                    page_offsets_json,raw_row_count,raw_rows_digest,
                    raw_rows_json,items_json,created_at
                ) VALUES(
                    ?,?,?,?,'[101]',1,1,'[0]',1,?,
                    '[{"nmID":101}]','[{"nm_id":101}]',?
                )
                """,
                (
                    "snapshot-" + historical_date,
                    version_id,
                    NOW,
                    historical_date,
                    "sha256:wb-" + historical_date,
                    NOW,
                ),
            )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,'base',?)",
            (NOW,),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
            ) VALUES(1,?,?,NULL,'base',?)
            """,
            (NOW, NOW, NOW),
        )
        conn.commit()


def _assert_business_time_projection(
    runtime: RegistryUploadDbBackedRuntime,
    applied: dict,
) -> None:
    publication = dict(applied.get("business_projection") or {})
    assert publication["business_effective_date"] == "2026-07-21", publication
    assert publication["affected_dates"] == [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-25",
    ], publication
    diagnostics = dict(publication.get("diagnostics") or {})
    assert diagnostics["missing_exact_functional_dates"] == [], diagnostics
    assert diagnostics["external_source_refresh_count"] == 0, diagnostics
    assert diagnostics["full_vitrina_refresh_count"] == 0, diagnostics
    assert diagnostics["all_history_rebuild"] is False, diagnostics
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute(
            f"SELECT 1 FROM {CURRENT_ROW_TABLE} WHERE as_of_date='2026-07-20'"
        ).fetchone() is None
        non_target = conn.execute(
            f"SELECT COUNT(*) FROM {CURRENT_ROW_TABLE} WHERE nm_id=202"
        ).fetchone()[0]
        assert non_target == 0
        rows = conn.execute(
            f"""
            SELECT as_of_date,metrics_json
            FROM {CURRENT_ROW_TABLE}
            WHERE nm_id=101
            ORDER BY as_of_date
            """
        ).fetchall()
    assert [row["as_of_date"] for row in rows] == publication["affected_dates"]
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        assert (
            metrics[own_stage_metric_key("PRODUCTION", "qty")] == 2.0
        ), (row["as_of_date"], metrics)
        assert (
            metrics[own_stage_metric_key("PRODUCTION_TO_FF", "qty")] == 10.0
        ), (row["as_of_date"], metrics)
        assert metrics[OWN_TOTAL_QTY_METRIC_KEY] == 12.0
    absorbed = OwnProductCapitalBlock(
        runtime=runtime,
        timestamp_factory=lambda: NOW,
    ).load_daily_metric_lookup("2026-07-21")
    assert (
        absorbed[101][own_stage_metric_key("PRODUCTION_TO_FF", "qty")]
        == 10.0
    ), absorbed


def _failure_is_atomic(runtime: RegistryUploadDbBackedRuntime) -> None:
    shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    runtime.save_supplier_shipment(
        header={
            **shipment["header"],
            "actual_shipment_date": None,
            "order_status": "production",
            "updated_at": "2026-07-25T10:01:00Z",
        },
        lines=shipment["lines"],
    )
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            "UPDATE sheet_vitrina_v1_warehouse_functional_active SET version_id='base' WHERE slot=1"
        )
        conn.commit()
        queue_before = conn.execute(
            """
            SELECT queue_id,stable_source_id,source_revision,effective_date,
                   affected_nm_ids_json,status,requested_at,started_at,
                   finished_at,error
            FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
            ORDER BY queue_id
            """
        ).fetchall()
    failing = SupplierShipmentFactualCorrectionBlock(
        runtime=runtime,
        timestamp_factory=lambda: "2026-07-25T10:02:00Z",
        failure_injector=lambda phase: (
            (_ for _ in ()).throw(RuntimeError("injected"))
            if phase == "before_commit"
            else None
        ),
    )
    with patch(
        "packages.application.warehouse_targeted_replay.load_supplier_line_cost_breakdown",
        side_effect=lambda **kwargs: _allocation(
            str(kwargs.get("actual_shipment_date_override") or "")
        ),
    ):
        plan = failing.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-07-21",
            actor="smoke",
            expected_old_value="",
        )
        try:
            failing.apply(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-07-21",
                actor="smoke",
                fingerprint=plan["fingerprint"],
                backup_dir=runtime.runtime_dir / "unused",
                expected_old_value="",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("failure injection did not abort")
    readback = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    assert not (readback.get("header") or {}).get("actual_shipment_date")
    with sqlite3.connect(runtime.db_path) as conn:
        assert (
            conn.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()[0]
            == "base"
        )
        queue_after = conn.execute(
            """
            SELECT queue_id,stable_source_id,source_revision,effective_date,
                   affected_nm_ids_json,status,requested_at,started_at,
                   finished_at,error
            FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
            ORDER BY queue_id
            """
        ).fetchall()
        assert queue_after == queue_before
    retrying = SupplierShipmentFactualCorrectionBlock(
        runtime=runtime,
        timestamp_factory=lambda: "2026-07-25T10:03:00Z",
    )
    with patch(
        "packages.application.warehouse_targeted_replay.load_supplier_line_cost_breakdown",
        side_effect=lambda **kwargs: _allocation(
            str(kwargs.get("actual_shipment_date_override") or "")
        ),
    ):
        retry = retrying.apply(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-07-21",
            actor="smoke",
            fingerprint=plan["fingerprint"],
            backup_dir=runtime.runtime_dir / "unused",
            expected_old_value="",
        )
    assert retry["applied"] is True
    assert (
        runtime.load_supplier_shipment(SHIPMENT_ID)["header"][
            "actual_shipment_date"
        ]
        == "2026-07-21"
    )
    with sqlite3.connect(runtime.db_path) as conn:
        statuses = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT status
                FROM sheet_vitrina_v1_warehouse_business_projection_revisions
                WHERE stable_source_id=?
                  AND source_revision=?
                """,
                (
                    f"supplier_shipment:{SHIPMENT_ID}",
                    plan["source_revision"],
                ),
            ).fetchall()
        }
    assert statuses == {"active", "failed"}, statuses


def _lock_is_shared(runtime: RegistryUploadDbBackedRuntime) -> None:
    started = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with warehouse_functional_write_lock(runtime.runtime_dir):
            started.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=holder)
    thread.start()
    started.wait(timeout=2)
    try:
        try:
            with warehouse_functional_write_lock(
                runtime.runtime_dir, timeout_seconds=0.05
            ):
                raise AssertionError("concurrent writer acquired shared lock")
        except WarehouseFunctionalBusyError:
            pass
    finally:
        release.set()
        thread.join(timeout=2)


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    main()
