#!/usr/bin/env python3
"""Production-shaped Stage 7C dry-run, apply, exact readback and retry smoke."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_cutover_production import (  # noqa: E402
    FfPoolCutoverProductionError,
    FfPoolCutoverProductionMutation,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    process_post_t_fbs_lifecycle,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional import (  # noqa: E402
    ensure_warehouse_functional_schema,
)


SHA = "c" * 40
GATE_AT = "2026-08-14T05:00:00Z"
CUTOVER_AT = "2026-08-14T05:05:00Z"
SHIPMENT_ID = "sup_adc29a3cba934403bca4842c2add8b7d"


class _Clock:
    def __init__(self) -> None:
        self.values = [GATE_AT, CUTOVER_AT, "2026-08-14T05:06:00Z"]
        self.at_cutover = None

    def __call__(self) -> str:
        value = self.values.pop(0) if self.values else "2026-08-14T05:07:00Z"
        if value == CUTOVER_AT and self.at_cutover is not None:
            callback, self.at_cutover = self.at_cutover, None
            callback()
        return value


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_warehouse_functional_schema(conn)
            _ensure_schema(conn)
            _seed(conn)
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
        assert gate["apply_allowed"] is True, gate["blockers"]
        assert gate["cutover_boundary"]["chosen"] is False
        assert gate["source"]["opening_summary"] == {
            "quantity": 8,
            "capital_rub": "79.995",
            "facility_id": "fac_moscow",
            "FBS": True,
            "FBO_opening_zero": True,
        }
        excluded = gate["source"]["excluded_pending_receipts"]
        assert excluded[0]["invoice_no"] == "26GN527"
        assert excluded[0]["expected_quantity"] == 66_000
        assert excluded[0]["opening_quantity"] == 0
        assert gate["handoff_policy"]["supplier_status_complete_alone_forbidden"] is True

        before_status = _status_digest(runtime.db_path)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_shipments "
                "SET actual_ff_acceptance_date='2026-08-14' WHERE shipment_id=?",
                (SHIPMENT_ID,),
            )
            conn.commit()
        try:
            runner.apply(
                gate,
                fingerprint=gate["fingerprint"],
                approval_reference="owner-gate-smoke-1",
                actor="smoke",
                backup_dir=root / "backups",
                external_barrier_evidence=_barrier(),
            )
            raise AssertionError("concurrent supplier acceptance was not detected")
        except FfPoolCutoverProductionError as exc:
            assert exc.code == "gate_source_drift"
        assert _pool_operation_count(runtime.db_path) == 0
        assert _status_digest(runtime.db_path) == before_status
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_shipments "
                "SET actual_ff_acceptance_date=NULL WHERE shipment_id=?",
                (SHIPMENT_ID,),
            )
            conn.commit()

        # Rebuild after the deliberately injected drift; the exact gate owns
        # the current source and T is still unbound.
        live_clock = _Clock()
        runner.timestamp_factory = live_clock
        gate = runner.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
        arrived_status: dict[str, str] = {}

        def _arrival_during_t() -> None:
            with sqlite3.connect(runtime.db_path) as arriving:
                _insert_during_t_order(arriving)
                arriving.commit()
            arrived_status["digest"] = _status_digest(runtime.db_path)

        live_clock.at_cutover = _arrival_during_t
        result = runner.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-gate-smoke-2",
            actor="smoke",
            backup_dir=root / "backups",
            external_barrier_evidence=_barrier(),
        )
        assert result["status"] == "applied_reconciled"
        assert result["readback"]["readback"]["status"] == "pass"
        assert result["apply"]["readback"]["reader_enabled"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            detail = [
                tuple(row)
                for row in conn.execute(
                    """SELECT nm_id,quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances
                       ORDER BY nm_id"""
                )
            ]
            assert detail == [(101, 10, "100"), (102, -2, "-20.005")]
            pending = conn.execute(
                """SELECT classification,expected_quantity,post_cutover_state
                   FROM sheet_vitrina_v1_ff_pool_cutover_pending_shipments
                   WHERE shipment_id=?""",
                (SHIPMENT_ID,),
            ).fetchone()
            assert tuple(pending) == ("excluded_pending_receipt", 66_000, "in_transit")
            assert conn.execute(
                "SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments "
                "WHERE shipment_id=?",
                (SHIPMENT_ID,),
            ).fetchone()[0] is None
        assert _status_digest(runtime.db_path) == arrived_status["digest"]
        assert (
            Path(result["backup"]["target_before_image"]["path"]).stat().st_mode
            & 0o777
            == 0o600
        )
        assert result["backup"]["warehouse_recovery"]["lifecycle"] == "retained"

        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            late = process_post_t_fbs_lifecycle(
                conn, occurred_at="2026-08-14T05:06:30Z", schema_ready=True
            )
            conn.commit()
        assert late["summary"]["late_pre_t"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases "
                "WHERE order_id=9002"
            ).fetchone()[0] == 1

        repeated = runner.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-gate-smoke-2",
            actor="smoke",
            backup_dir=root / "backups",
            external_barrier_evidence=_barrier(),
        )
        assert repeated["status"] == "already_applied_reconciled"
        assert repeated["idempotent"] is True
        assert _pool_operation_count(runtime.db_path) == 1
        _assert_precommit_crash_recovery(root / "crash-recovery")
    print("ff_pool_cutover_production_smoke: OK")
    return 0


def _assert_precommit_crash_recovery(root: Path) -> None:
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        _ensure_schema(conn)
        _seed(conn)
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
    try:
        runner.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-gate-crash",
            actor="smoke",
            backup_dir=root / "backups",
            external_barrier_evidence=_barrier(),
            crash="before_commit",
        )
    except RuntimeError as exc:
        assert "crash before commit" in str(exc)
    else:
        raise AssertionError("pre-commit crash injection did not fail")
    with sqlite3.connect(runtime.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_cutover_manifests"
        ).fetchone()[0] == 0
        assert _pool_operation_count(runtime.db_path) == 0
        assert conn.execute(
            "SELECT phase FROM sheet_vitrina_v1_warehouse_domain_write_epoch_events "
            "ORDER BY event_sequence DESC LIMIT 1"
        ).fetchone()[0] == "aborted"
        assert conn.execute(
            "SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations "
            "WHERE operation_kind='warehouse_opening_publication'"
        ).fetchone()[0] == "failed_recoverable"
    recovered = runner.apply(
        gate,
        fingerprint=gate["fingerprint"],
        approval_reference="owner-gate-crash",
        actor="smoke",
        backup_dir=root / "backups",
        external_barrier_evidence=_barrier(),
    )
    assert recovered["status"] == "applied_reconciled"
    assert recovered["readback"]["readback"]["status"] == "pass"
    assert _pool_operation_count(runtime.db_path) == 1
    with sqlite3.connect(runtime.db_path) as conn:
        phases = conn.execute(
            "SELECT epoch_id,phase FROM sheet_vitrina_v1_warehouse_domain_write_epoch_events "
            "ORDER BY event_sequence"
        ).fetchall()
        assert phases[0][1] == "held" and phases[1][1] == "aborted"
        assert phases[-1][1] == "released"
        assert phases[0][0] != phases[-1][0]
        assert conn.execute(
            "SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations "
            "WHERE operation_kind='warehouse_opening_publication'"
        ).fetchone()[0] == "retained"


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_ff_facilities VALUES(?,?,?,?,?,?,?)",
        (
            "fac_moscow", "MSK", "FF Москва", 1, "Asia/Yekaterinburg",
            GATE_AT, GATE_AT,
        ),
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_ff_facility_profiles VALUES(?,?,?,?,?)",
        ("fac_moscow", "Москва", "{}", GATE_AT, GATE_AT),
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,?,?)",
        ("wf_stage7c", GATE_AT),
    )
    balances = [
        (101, "10", "10", "100"),
        (102, "-2", "10.0025", "-20.005"),
    ]
    for nm_id, quantity, wac, capital in balances:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                   version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client,provenance_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "wf_stage7c", "ff", nm_id, quantity, wac, capital, quantity,
                "exact", 1, "0", "0", "0", "{}",
            ),
        )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_supplier_shipments(
               shipment_id,created_at,updated_at,shipment_date,actual_shipment_date,
               actual_ff_acceptance_date,order_status,invoice_no,product_qty_total,
               match_status,warnings_json,errors_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            SHIPMENT_ID, GATE_AT, GATE_AT, "2026-07-20", "2026-07-25", None,
            "in_transit", "26GN527", 66_000, "matched", "[]", "[]",
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
               line_id,shipment_id,line_type,sort_order,internal_nm_id,qty,
               manual_override,price_conformity_status,price_conformity_check_mode,
               price_conformity_reason,price_conformity_context_json,raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "line_26gn527_1", SHIPMENT_ID, "product", 1, 101, 66_000,
            0, "not_checked", "not_checked", "not_checked", "{}", "{}",
        ),
    )
    warehouse_digest = "sha256:" + "1" * 64
    identity_mapping_digest = "sha256:" + "2" * 64
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings(
               mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
               created_at,created_by
           ) VALUES(?,?,?,?,?,?,?)""",
        ("warehouse_mapping_1", 501, "fac_moscow", warehouse_digest, 1, GATE_AT, "smoke"),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_mappings(
               mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,
               target_nm_id,mapping_digest,active,created_at,created_by
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "identity_mapping_1", 101, 201, "sku-101", "seller-101", 101,
            identity_mapping_digest, 1, GATE_AT, "smoke",
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
               observation_id,order_id,source_revision,supply_id,delivery_type,
               source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
               skus_json,observed_at,collector_date_from,collector_date_to,collector_cursor
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "order_observation_9001", 9001, "order_revision_9001", "supply-fbs",
            "fbs", "2026-08-14T04:00:00Z", 501, 601, 101, 201,
            "seller-101", '["sku-101"]', "2026-08-14T04:01:00Z", 1, 2, 0,
        ),
    )
    status_digest = "sha256:" + "3" * 64
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
               observation_id,order_id,order_revision,status_digest,supplier_status,
               wb_status,positive_quantity,observed_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "status_observation_9001", 9001, "order_revision_9001", status_digest,
            "complete", "waiting", 1, "2026-08-14T04:02:00Z",
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
               evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
               barcode,seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,
               evidence_digest,observed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "identity_evidence_9001", 9001, "order_revision_9001", 501, 101, 201,
            "sku-101", "seller-101", "matched", "warehouse_mapping_1",
            "identity_mapping_1", "sha256:" + "4" * 64, "2026-08-14T04:03:00Z",
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_collector_state(
               state_id,last_run_id,last_status,last_attempt_at,last_success_at,
               window_date_from,window_date_to,next_cursor,complete
           ) VALUES(1,?,?,?,?,?,?,?,1)""",
        ("collector_run_1", "success", GATE_AT, GATE_AT, 1, 2, 0),
    )


def _barrier() -> dict[str, object]:
    return {
        "maintenance_quiet": True,
        "http_write_barrier_active": True,
        "warehouse_timer_held": True,
        "warehouse_lock_held": False,
        "supplier_acceptance_writer_held": True,
        "fbs_collector_continues": True,
        "canonical_target": True,
    }


def _insert_during_t_order(conn: sqlite3.Connection) -> None:
    revision = "order_revision_9002"
    status_digest = "sha256:" + "8" * 64
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
               observation_id,order_id,source_revision,supply_id,delivery_type,
               source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
               skus_json,observed_at,collector_date_from,collector_date_to,collector_cursor
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "order_observation_9002", 9002, revision, "supply-fbs", "fbs",
            "2026-08-14T05:04:59Z", 501, 601, 101, 201, "seller-101",
            '["sku-101"]', CUTOVER_AT, 1, 2, 0,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
               observation_id,order_id,order_revision,status_digest,supplier_status,
               wb_status,positive_quantity,observed_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "status_observation_9002", 9002, revision, status_digest,
            "new", "waiting", 1, CUTOVER_AT,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_current(
               order_id,order_revision,status_digest,supplier_status,wb_status,
               source_observed_at,local_first_seen_at,local_last_seen_at,
               observation_count,episode_sequence
           ) VALUES(?,?,?,?,?,'',?,?,1,1)""",
        (9002, revision, status_digest, "new", "waiting", CUTOVER_AT, CUTOVER_AT),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
               evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
               barcode,seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,
               evidence_digest,observed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "identity_evidence_9002", 9002, revision, 501, 101, 201,
            "sku-101", "seller-101", "matched", "warehouse_mapping_1",
            "identity_mapping_1", "sha256:" + "9" * 64, CUTOVER_AT,
        ),
    )


def _pool_operation_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0]
        )


def _status_digest(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """SELECT observation_sequence,observation_id,order_id,order_revision,
                      status_digest,supplier_status,wb_status,positive_quantity,observed_at
               FROM sheet_vitrina_v1_wb_supplies_fbs_status_observations
               ORDER BY observation_sequence"""
        ).fetchall()
    return json.dumps(rows, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
