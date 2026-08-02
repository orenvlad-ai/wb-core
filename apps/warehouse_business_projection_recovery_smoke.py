#!/usr/bin/env python3
"""Exact-functional projection recovery, idempotency and T1 rollback smoke."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_inventory_reconciliation import (  # noqa: E402
    ensure_inventory_reconciliation_schema,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    own_stage_total_metric_key,
)
from packages.application.warehouse_business_projection import (  # noqa: E402
    CURRENT_ROW_TABLE,
    STATE_TABLE,
    _persist_projection_revision,
    ensure_warehouse_business_projection_schema,
)
from packages.application.warehouse_business_projection_recovery import (  # noqa: E402
    apply_business_projection_recovery_plan,
    build_business_projection_recovery_plan,
    rollback_business_projection_recovery,
)
from packages.application.warehouse_functional import (  # noqa: E402
    _watermark,
    ensure_warehouse_functional_schema,
)


SOURCE_SHA = "sha256:" + hashlib.sha256(b"manager-workbook").hexdigest()
BUSINESS_DATE = "2026-07-31"


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-projection-recovery-") as temp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp) / "runtime"
        )
        runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        _seed(runtime)
        plan = build_business_projection_recovery_plan(
            runtime,
            source_sha256=SOURCE_SHA,
            business_date=BUSINESS_DATE,
        )
        assert plan["status"] == "ready" and plan["would_change"] is True, plan
        assert plan["target_dates"] == [
            "2026-07-30",
            "2026-07-31",
            "2026-08-01",
            "2026-08-02",
        ]
        by_date = {
            item["business_date"]: item for item in plan["versions"]
        }
        assert by_date["2026-07-30"]["stage_totals"]["ff"] == {
            "quantity": "10",
            "capital_rub": "1000",
        }
        for day in ("2026-07-31", "2026-08-01", "2026-08-02"):
            assert by_date[day]["stage_totals"]["ff"] == {
                "quantity": "12",
                "capital_rub": "1200",
            }
        assert len(by_date["2026-07-31"]["missing_operation_ids_applied"]) == 3
        assert len(by_date["2026-08-01"]["missing_operation_ids_applied"]) == 3
        assert by_date["2026-08-02"]["missing_operation_ids_applied"] == []
        physical_before = plan["non_target_digest"]

        applied = apply_business_projection_recovery_plan(
            runtime,
            plan,
            confirm_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:projection-recovery-smoke",
        )
        assert applied["applied"] is True and applied["idempotent"] is False
        assert applied["readback"]["non_target_unchanged"] is True
        assert applied["readback"]["non_target_digest"] == physical_before
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            state = conn.execute(
                f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
            ).fetchone()
            assert str(state["business_effective_date"]) == "2026-08-02"
            assert str(state["revision_id"]) == plan["revision_id"]
            total_rows = conn.execute(
                f"SELECT as_of_date,metrics_json FROM {CURRENT_ROW_TABLE} "
                "WHERE nm_id=0 ORDER BY as_of_date"
            ).fetchall()
            assert len(total_rows) == 4
            metrics = {
                str(row["as_of_date"]): json.loads(row["metrics_json"])
                for row in total_rows
            }
            ff_qty_key = own_stage_total_metric_key("FF", "qty")
            ff_capital_key = own_stage_total_metric_key("FF", "capital_rub")
            assert metrics["2026-07-30"][ff_qty_key] == 10.0
            assert metrics["2026-07-31"][ff_qty_key] == 12.0
            assert metrics["2026-08-02"][ff_capital_key] == 1200.0

        repeated = apply_business_projection_recovery_plan(
            runtime,
            plan,
            confirm_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:projection-recovery-smoke",
        )
        assert repeated["idempotent"] is True
        assert repeated["second_run"] == {
            "tier": "T0",
            "changed_rows": 0,
            "mutations": 0,
            "recovery_bytes": 0,
        }

        rolled_back = rollback_business_projection_recovery(
            runtime,
            fingerprint=plan["fingerprint"],
            reason="smoke rollback proof",
        )
        assert rolled_back["lifecycle"] == "rolled_back"
        with sqlite3.connect(runtime.db_path) as conn:
            stale = conn.execute(
                f"SELECT metrics_json FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date='2026-07-31' AND nm_id=0"
            ).fetchone()
            assert (
                json.loads(stale[0])[OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY]
                == 999.0
            )
    print("warehouse_business_projection_recovery_smoke: OK")


def _seed(runtime: RegistryUploadDbBackedRuntime) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        ensure_warehouse_functional_schema(conn)
        ensure_inventory_reconciliation_schema(conn)
        ensure_warehouse_business_projection_schema(conn)
        opening_operation = _operation("ff-opening", "2026-07-29T10:00:00Z", "opening")
        _insert_operation(conn, opening_operation)
        pre_watermark = _watermark(_operation_rows(conn), "created_at")
        for day in ("2026-07-30", "2026-07-31", "2026-08-01"):
            _insert_version(
                conn,
                day=day,
                version_id="version-" + day,
                ff_quantity="10",
                ff_capital="1000",
                watermark=pre_watermark,
                active=False,
            )

        documents = [
            ("ff-return", "2026-08-02T10:00:00Z", "auto_return", "2", "200"),
            ("ff-receipt", "2026-08-02T10:00:01Z", "inventory_receipt", "1", "100"),
            ("ff-writeoff", "2026-08-02T10:00:02Z", "inventory_writeoff", "-1", "-100"),
        ]
        for operation_id, created_at, operation_type, quantity, capital in documents:
            operation = _operation(operation_id, created_at, operation_type)
            _insert_operation(conn, operation)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                    operation_id,line_no,nm_id,barcode,sku,nomenclature_name,
                    comment,group_name,quantity_delta,raw_json
                ) VALUES(?,1,101,'101','SKU-101','SKU 101','','',?,?)
                """,
                (
                    operation_id,
                    quantity,
                    json.dumps(
                        {
                            "cost_snapshot": {
                                "unit_cost_rub": "100",
                                "capital_delta_rub": capital,
                                "quality": "exact_frozen_cost",
                                "provenance": {"fixture": True},
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
        current_watermark = _watermark(_operation_rows(conn), "created_at")
        _insert_version(
            conn,
            day="2026-08-02",
            version_id="version-2026-08-02",
            ff_quantity="12",
            ff_capital="1200",
            watermark=current_watermark,
            active=True,
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_inventory_reconciliations(
                reconciliation_id,source_sha256,source_filename,source_content_type,
                source_file_blob,business_date,plan_fingerprint,manifest_json,
                approval_reference,created_by,created_at,status,operation_ids_json,
                before_digest,non_target_digest,after_digest,reconciliation_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "reconciliation-1",
                SOURCE_SHA,
                "manager.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"manager-workbook",
                BUSINESS_DATE,
                "sha256:inventory-plan",
                json.dumps(
                    {
                        "target_total": "12",
                        "per_sku": [{"nm_id": 101, "target_quantity": "12"}],
                    },
                    sort_keys=True,
                ),
                "github-comment:inventory-gate",
                "smoke",
                "2026-08-02T10:00:03Z",
                "applied",
                json.dumps([item[0] for item in documents]),
                "sha256:before",
                "sha256:non-target",
                "sha256:after",
                "{}",
            ),
        )
        stale_rows = []
        for day in ("2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02"):
            material = {
                "as_of_date": day,
                "nm_id": 0,
                "metrics": {OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY: 999.0},
                "presentation": {},
                "provenance": {"source": "partial_event_projection"},
            }
            stale_rows.append(
                {
                    **material,
                    "row_fingerprint": "sha256:stale-" + day,
                }
            )
        _persist_projection_revision(
            conn,
            revision_id="stale-revision",
            stable_source_id="stale-source",
            source_revision="sha256:stale",
            business_effective_date="2026-07-24",
            published_at="2026-08-02T10:00:04Z",
            plan_fingerprint="sha256:stale-plan",
            base_version_id="",
            published_version_id="partial-events",
            affected_nm_ids=[101],
            source_kind="coalesced_capital_events",
            rows=stale_rows,
            diagnostics={"affected_dates": [item["as_of_date"] for item in stale_rows]},
        )
        conn.commit()


def _operation(operation_id: str, created_at: str, operation_type: str) -> dict:
    return {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "source_type": "inventory_reconciliation",
        "source_key": "source:" + operation_id,
        "source_object_id": "reconciliation-1",
        "source_object_label": "fixture",
        "created_at": created_at,
        "business_effective_date": BUSINESS_DATE,
        "created_by": "smoke",
        "sku_count": 1,
        "total_quantity_delta": 0,
        "total_quantity_abs": 0,
        "warnings_json": "[]",
        "diagnostics_json": "{}",
        "source_filename": None,
        "source_content_type": None,
        "source_file_sha256": None,
        "source_file_blob": None,
    }


def _insert_operation(conn: sqlite3.Connection, item: dict) -> None:
    columns = ",".join(item)
    placeholders = ",".join("?" for _ in item)
    conn.execute(
        f"INSERT INTO sheet_vitrina_v1_ff_stock_operations({columns}) "
        f"VALUES({placeholders})",
        tuple(item.values()),
    )


def _operation_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operations "
            "ORDER BY created_at,operation_id"
        ).fetchall()
    ]


def _insert_version(
    conn: sqlite3.Connection,
    *,
    day: str,
    version_id: str,
    ff_quantity: str,
    ff_capital: str,
    watermark: dict,
    active: bool,
) -> None:
    created_at = day + "T12:00:00Z"
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
            version_id,cutover_id,version_kind,effective_at,status,
            plan_fingerprint,local_source_digest,source_watermarks_json,
            created_at,business_effective_date,published_at
        ) VALUES(?,'warehouse_functional_cutover_v1','hourly_wb_sync',?,'good',?,?,?,?,?,?)
        """,
        (
            version_id,
            created_at,
            "sha256:plan-" + day,
            "sha256:local-" + day,
            json.dumps({"ff_ledger": watermark}, sort_keys=True, default=str),
            created_at,
            day,
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
            snapshot_id,version_id,fetched_at,snapshot_date,
            requested_nm_ids_json,pagination_complete,page_count,
            page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,
            items_json,created_at
        ) VALUES(?,?,?,?,?,1,1,'[0]',1,?,'[]','[]',?)
        """,
        (
            "snapshot-" + day,
            version_id,
            created_at,
            day,
            "[101]",
            "sha256:snapshot-" + day,
            created_at,
        ),
    )
    balances = [
        ("production", "10", "50", "500"),
        ("ff", ff_quantity, "100", ff_capital),
        ("ff_to_wb", "5", "100", "500"),
        ("wb", "20", "150", "3000"),
    ]
    for stage, quantity, wac, capital in balances:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES(?,?,?,?,?,?,?,'certified',1,'0','0','0','{}')
            """,
            (version_id, stage, 101, quantity, wac, capital, quantity),
        )
    if active:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_active(
                slot,version_id,updated_at
            ) VALUES(1,?,?)
            """,
            (version_id, created_at),
        )


if __name__ == "__main__":
    main()
