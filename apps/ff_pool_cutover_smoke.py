#!/usr/bin/env python3
"""Deterministic Stage 6 planning, guard, atomicity and recovery smoke."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_cutover import (
    FfPoolCutoverAmbiguousCommit,
    FfPoolCutoverError,
    FIXTURE_MARKER_TABLE,
    OPENING_RESERVATIONS_TABLE,
    ORDERS_TABLE,
    _apply_ff_pool_cutover_fixture,
    _ensure_order_classification_schema,
    _fingerprint,
    build_ff_pool_cutover_plan,
    classify_late_pre_t_observations,
    ensure_ff_pool_cutover_schema,
    ff_pool_fbs_accounting_boundary_snapshot,
    ff_pool_cutover_preflight_snapshot,
    read_ff_pool_cutover_status,
    read_ff_pool_cutover_readback,
)
from packages.application.ff_pool_foundation import FACILITIES_TABLE
from packages.application.warehouse_domain_write_guard import (
    EVENTS_TABLE,
    WAREHOUSE_DOMAIN_TABLES,
    install_warehouse_domain_table_guards,
)


SHA = "a" * 40
T = "2026-08-12T05:00:00Z"
DIGEST = "sha256:" + "b" * 64


def _legacy_order_schema_db(*, ambiguous: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    classifications = (
        "'pre_t_absorbed_closed','pre_t_absorbed_reservation',"
        "'post_t_deferred','unmatched'"
    )
    if ambiguous:
        classifications += ",'unexpected_class'"
    conn.executescript(
        f"""
        CREATE TABLE sheet_vitrina_v1_ff_pool_cutover_manifests(
            cutover_id TEXT PRIMARY KEY
        );
        CREATE TABLE sheet_vitrina_v1_ff_facilities(
            facility_id TEXT PRIMARY KEY
        );
        CREATE TABLE {ORDERS_TABLE}(
            cutover_id TEXT NOT NULL
                REFERENCES sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            observation_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_created_at TEXT NOT NULL,
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            classification TEXT NOT NULL CHECK(classification IN ({classifications})),
            facility_id TEXT REFERENCES sheet_vitrina_v1_ff_facilities(facility_id),
            pool TEXT CHECK(pool IS NULL OR pool='FBS'),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>=0),
            status_fingerprint TEXT NOT NULL,
            mapping_digest TEXT NOT NULL,
            observation_sequence INTEGER NOT NULL DEFAULT 0,
            status_observation_sequence INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(cutover_id,order_id)
        );
        CREATE INDEX ff_pool_cutover_orders_by_class
        ON {ORDERS_TABLE}(cutover_id,classification,order_id);
        CREATE TABLE {OPENING_RESERVATIONS_TABLE}(
            cutover_id TEXT NOT NULL
                REFERENCES sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            order_id INTEGER NOT NULL,
            nm_id INTEGER NOT NULL,
            PRIMARY KEY(cutover_id,order_id,nm_id),
            FOREIGN KEY(cutover_id,order_id)
                REFERENCES {ORDERS_TABLE}(cutover_id,order_id)
        );
        INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES('cutover_legacy');
        INSERT INTO sheet_vitrina_v1_ff_facilities VALUES('facility_legacy');
        INSERT INTO {ORDERS_TABLE}(
            cutover_id,order_id,observation_id,source_revision,source_created_at,
            observed_at,classification,facility_id,pool,nm_id,quantity,
            status_fingerprint,mapping_digest,observation_sequence,
            status_observation_sequence
        ) VALUES(
            'cutover_legacy',7001,'observation_legacy','revision_legacy',
            '2026-08-12T04:00:00Z','2026-08-12T04:01:00Z',
            'pre_t_absorbed_reservation','facility_legacy','FBS',101,1,
            '{DIGEST}','{DIGEST}',11,12
        );
        INSERT INTO {OPENING_RESERVATIONS_TABLE} VALUES('cutover_legacy',7001,101);
        """
    )
    conn.commit()
    return conn


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(
            slot INTEGER PRIMARY KEY,version_id TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(
            version_id TEXT NOT NULL,warehouse_key TEXT NOT NULL,nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,wac_rub TEXT,capital_rub TEXT NOT NULL,
            PRIMARY KEY(version_id,warehouse_key,nm_id));
        CREATE TABLE sheet_vitrina_v1_wb_supplies(
            supply_id TEXT PRIMARY KEY,cache_key TEXT,wb_supply_id TEXT,status_id INTEGER,
            raw_list_hash TEXT,raw_detail_hash TEXT,raw_goods_hash TEXT,raw_package_hash TEXT);
        CREATE TABLE sheet_vitrina_v1_ff_stock_operations(
            operation_id TEXT PRIMARY KEY,source_key TEXT UNIQUE);
        CREATE TABLE sheet_vitrina_v1_supplier_shipments(
            shipment_id TEXT PRIMARY KEY,invoice_no TEXT,actual_shipment_date TEXT,
            actual_ff_acceptance_date TEXT,product_qty_total REAL,archived_at TEXT,
            order_status TEXT,updated_at TEXT);
        CREATE TABLE sheet_vitrina_v1_supplier_shipment_lines(
            line_id TEXT PRIMARY KEY,shipment_id TEXT,line_type TEXT,qty REAL);
        CREATE TABLE sheet_vitrina_v1_supplier_ff_cost_layers(
            layer_id TEXT PRIMARY KEY,supplier_shipment_id TEXT);
        """
    )
    ensure_ff_pool_cutover_schema(conn)
    conn.execute(
        f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,?,?,?)",
        ("facility_test", "TEST", "Test facility", 1, "Asia/Yekaterinburg", T, T),
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'wf_test',?)", (T,)
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES(?,?,?,?,?,?)",
        ("wf_test", "ff", 101, "10", "10", "100"),
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_supplier_shipments VALUES(?,?,?,?,?,?,?,?)",
        (
            "china_shipment_0001", "26GN527", "2026-08-01", "", 66,
            "", "in_transit", T,
        ),
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_supplier_shipment_lines VALUES(?,?,?,?)",
        ("supplier_line_0001", "china_shipment_0001", "product", 66),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
            observation_id,order_id,source_revision,supply_id,delivery_type,
            source_created_at,warehouse_id,office_id,nm_id,chrt_id,skus_json,
            observed_at,collector_date_from,collector_date_to,collector_cursor
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "observation_0001", 7001, "source_revision_0001", "supply_fbs_1", "fbs",
            "2026-08-12T04:00:00Z", 501, 601, 101, 201, '["sku-101"]',
            "2026-08-12T04:01:00Z", 1, 2, 0,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_collector_state(
            state_id,last_run_id,last_status,last_attempt_at,last_success_at,
            window_date_from,window_date_to,next_cursor,complete
        ) VALUES(1,'run_0001','success',?,?,?,?,?,1)""",
        (T, T, 1, 2, 0),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_ff_pool_cutover_order_status_evidence(
            order_id,source_revision,evidence_digest,lifecycle_class,quantity,observed_at
        ) VALUES(7001,'source_revision_0001',?,'active_pre_handoff',1,?)""",
        (DIGEST, T),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
            observation_id,order_id,order_revision,status_digest,supplier_status,
            wb_status,positive_quantity,observed_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "status_observation_0001", 7001, "source_revision_0001", DIGEST,
            "complete", "waiting", 1, "2026-08-12T04:02:00Z",
        ),
    )
    install_warehouse_domain_table_guards(conn)
    conn.commit()
    return conn


def _proposal(conn: sqlite3.Connection) -> dict:
    accounting_boundary = ff_pool_fbs_accounting_boundary_snapshot(
        conn, boundary_at=T
    )
    warehouses = [{"warehouse_id": 501, "facility_id": "facility_test", "evidence_digest": DIGEST}]
    sku_identity = _fingerprint({"nm_id": 101, "chrt_id": 201, "skus": ["sku-101"]})
    skus = [{
        "nm_id": 101,
        "source_nm_id": 101,
        "target_nm_id": 101,
        "chrt_id": 201,
        "identity_digest": sku_identity,
    }]
    mapping_digest = _fingerprint({"warehouses": warehouses, "skus": skus})
    classification = {
        "order_id": 7001,
        "observation_sequence": 1,
        "status_observation_sequence": 1,
        "observation_id": "observation_0001",
        "source_revision": "source_revision_0001",
        "source_created_at": "2026-08-12T04:00:00Z",
        "observed_at": "2026-08-12T04:01:00Z",
        "classification": "pre_t_absorbed_reservation",
        "facility_id": "facility_test",
        "pool": "FBS",
        "nm_id": 101,
        "quantity": 1,
        "status_fingerprint": DIGEST,
        "status_evidence": {
            "observation_sequence": 1,
            "source_revision": "source_revision_0001",
            "status_digest": DIGEST,
            "supplier_status": "complete",
            "wb_status": "waiting",
            "quantity": 1,
            "observed_at": "2026-08-12T04:02:00Z",
        },
        "post_handoff_reconciliation": None,
        "mapping_digest": mapping_digest,
    }
    observation_digest = _fingerprint({"watermark": 1, "classifications": [classification]})
    preflight = ff_pool_cutover_preflight_snapshot(conn)
    shipment_evidence = _fingerprint(
        {
            "shipment_id": "china_shipment_0001",
            "invoice_no": "26GN527",
            "actual_shipment_date": "2026-08-01",
            "actual_ff_acceptance_date": "",
            "shipment_quantity": 66,
            "product_line_count": 1,
            "product_line_quantity": 66,
            "receipt_operation_count": 0,
            "cost_layer_count": 0,
        }
    )
    return {
        "contract_name": "ff_facility_pool_cutover_proposal_v1",
        "cutover_id": "cutover_test_0001",
        "business_date": "2026-08-12",
        "target_feature_epoch": 1,
        "write_epoch_id": "write_epoch_test_0001",
        "control_manifest_digest": DIGEST,
        "control_evidence": {
            "maintenance_quiet": True,
            "http_write_barrier_active": True,
            "warehouse_timer_held": True,
            "warehouse_lock_held": False,
            "evidence_digest": DIGEST,
        },
        "handoff_policy": {
            "decision": "approved",
            "supplier_status": "complete",
            "wb_status": "sorted",
            "approval_reference": "owner_gate_smoke_0001",
            "observed_complete_waiting_to_complete_sorted_distinct_orders": 75,
        },
        "allocations": [
            {"facility_id": "facility_test", "pool": "FBS", "nm_id": 101, "quantity": 6, "capital_rub": "60.00"},
            {"facility_id": "facility_test", "pool": "FBO", "nm_id": 101, "quantity": 4, "capital_rub": "40.00"},
        ],
        "order_classifications": [{
            "order_id": 7001, "classification": "pre_t_absorbed_reservation",
            "observation_sequence": 1, "status_observation_sequence": 1,
            "facility_id": "facility_test", "quantity": 1,
            "status_fingerprint": DIGEST,
            "status_evidence": classification["status_evidence"],
            "post_handoff_reconciliation": None,
            "mapping_digest": mapping_digest,
        }],
        "seller_warehouse_mappings": warehouses,
        "sku_mappings": skus,
        "fbw_origin_assignments": [],
        "china_shipments": [{
            "shipment_id": "china_shipment_0001", "facility_id": "facility_test",
            "classification": "excluded_pending_receipt",
            "pools": ["FBS", "FBO"], "evidence_digest": shipment_evidence,
        }],
        "collector_checkpoint": {
            "accounting_boundary_at": T,
            "observation_watermark_sequence": 1,
            "observation_watermark_digest": observation_digest,
            "status_observation_watermark_sequence": 1,
            "status_transition_watermark_sequence": 0,
            "frozen_evidence_digest": accounting_boundary["frozen_evidence_digest"],
        },
        "non_target_evidence_digest": preflight["non_target"]["digest"],
    }


def _hold(conn: sqlite3.Connection, proposal: dict) -> None:
    conn.execute(
        f"INSERT INTO {EVENTS_TABLE}(epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (proposal["write_epoch_id"], "held", proposal["control_manifest_digest"], SHA, T, "smoke", "{}"),
    )
    conn.commit()


def main() -> int:
    legacy_schema = _legacy_order_schema_db()
    before_order = dict(legacy_schema.execute(f"SELECT * FROM {ORDERS_TABLE}").fetchone())
    before_reservation = dict(
        legacy_schema.execute(f"SELECT * FROM {OPENING_RESERVATIONS_TABLE}").fetchone()
    )
    before_fk = tuple(
        tuple(row)
        for row in legacy_schema.execute(
            f"PRAGMA foreign_key_list({OPENING_RESERVATIONS_TABLE})"
        ).fetchall()
    )
    _ensure_order_classification_schema(legacy_schema)
    assert dict(legacy_schema.execute(f"SELECT * FROM {ORDERS_TABLE}").fetchone()) == before_order
    assert (
        dict(legacy_schema.execute(f"SELECT * FROM {OPENING_RESERVATIONS_TABLE}").fetchone())
        == before_reservation
    )
    assert tuple(
        tuple(row)
        for row in legacy_schema.execute(
            f"PRAGMA foreign_key_list({OPENING_RESERVATIONS_TABLE})"
        ).fetchall()
    ) == before_fk
    widened_sql = str(
        legacy_schema.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (ORDERS_TABLE,)
        ).fetchone()[0]
    )
    assert "pre_t_handoff_debit" in widened_sql and "pre_t_cancelled_noop" in widened_sql
    assert legacy_schema.execute("PRAGMA foreign_key_check").fetchall() == []
    legacy_schema.execute(
        f"""INSERT INTO {ORDERS_TABLE}(
                cutover_id,order_id,observation_sequence,status_observation_sequence,
                observation_id,source_revision,source_created_at,observed_at,
                classification,facility_id,pool,nm_id,quantity,status_fingerprint,mapping_digest
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cutover_legacy", 7002, 13, 14, "observation_handoff", "revision_handoff",
            "2026-08-12T04:02:00Z", "2026-08-12T04:03:00Z", "pre_t_handoff_debit",
            "facility_legacy", "FBS", 101, 1, DIGEST, DIGEST,
        ),
    )
    legacy_schema.commit()
    _ensure_order_classification_schema(legacy_schema)
    assert legacy_schema.execute(f"SELECT COUNT(*) FROM {ORDERS_TABLE}").fetchone()[0] == 2
    ambiguous_schema = _legacy_order_schema_db(ambiguous=True)
    try:
        _ensure_order_classification_schema(ambiguous_schema)
        raise AssertionError("ambiguous legacy order-classification CHECK accepted")
    except FfPoolCutoverError as exc:
        assert exc.code == "order_classification_schema_ambiguous"

    conn = _db()
    proposal = _proposal(conn)
    allocation_sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='sheet_vitrina_v1_ff_pool_cutover_allocation_lines'"
        ).fetchone()[0]
    )
    assert "quantity>=0" not in allocation_sql.replace(" ", "")
    legacy = sqlite3.connect(":memory:")
    absent = build_ff_pool_cutover_plan(legacy, proposal=proposal, deployed_sha=SHA)
    assert absent["status"] == "schema_absent"
    query_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations "
            "WHERE order_id=? ORDER BY observation_sequence DESC LIMIT 1",
            (7001,),
        ).fetchall()
    )
    assert "wb_fbs_observations_by_order" in query_plan
    waiting = build_ff_pool_cutover_plan(conn, proposal=proposal, deployed_sha=SHA)
    assert waiting["status"] == "awaiting_boundary" and waiting["apply_surface_available"] is False
    _hold(conn, proposal)
    plan = build_ff_pool_cutover_plan(conn, proposal=proposal, deployed_sha=SHA, cutover_at=T)
    assert plan["status"] == "ready", plan["blockers"]
    assert plan["manifest"]["invariants"]["supplier_status_complete_never_debits"] is True
    assert plan["manifest"]["china_shipments"][0]["classification"] == "excluded_pending_receipt"
    wrong = copy.deepcopy(proposal)
    wrong["allocations"][0]["quantity"] = 5
    blocked = build_ff_pool_cutover_plan(conn, proposal=wrong, deployed_sha=SHA, cutover_at=T)
    assert blocked["status"] == "blocked"
    try:
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_ff_stock_operations(operation_id) VALUES('forbidden')"
        )
        raise AssertionError("canonical FF writer bypassed barrier")
    except sqlite3.IntegrityError as exc:
        assert "warehouse domain write barrier active" in str(exc)
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
            observation_id,order_id,source_revision,delivery_type,nm_id,observed_at,
            collector_date_from,collector_date_to,collector_cursor
        ) VALUES('observation_0002',7002,'source_revision_0002','fbs',101,?,1,2,0)""",
        (T,),
    )
    conn.rollback()
    guards = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    existing_tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table in set(WAREHOUSE_DOMAIN_TABLES) & existing_tables:
        suffix = table.removeprefix("sheet_vitrina_v1_")
        assert all(f"warehouse_domain_guard_{suffix}_{action}" in guards for action in ("insert", "update", "delete"))

    atomic = _db()
    atomic_proposal = _proposal(atomic)
    _hold(atomic, atomic_proposal)
    atomic.execute(f"CREATE TABLE {FIXTURE_MARKER_TABLE}(marker INTEGER)")
    atomic.commit()
    try:
        _apply_ff_pool_cutover_fixture(
            atomic, proposal=atomic_proposal, deployed_sha=SHA, cutover_at=T, crash="before_commit"
        )
        raise AssertionError("before-commit crash not injected")
    except RuntimeError:
        pass
    assert read_ff_pool_cutover_status(atomic)["status"] == "not_applied"
    result = _apply_ff_pool_cutover_fixture(atomic, proposal=atomic_proposal, deployed_sha=SHA, cutover_at=T)
    assert result["status"] == "applied_fixture"
    status = read_ff_pool_cutover_status(atomic)
    assert status["status"] == "applied_unreleased"
    assert status["counts"]["opening_reservations"] == 1
    assert read_ff_pool_cutover_readback(atomic, cutover_id="cutover_test_0001")["status"] == "pass"
    atomic.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
            observation_id,order_id,source_revision,delivery_type,source_created_at,
            nm_id,observed_at,collector_date_from,collector_date_to,collector_cursor
        ) VALUES('observation_late',7003,'source_revision_late','fbs',?,101,?,1,2,0)""",
        ("2026-08-12T04:30:00Z", T),
    )
    atomic.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
            observation_id,order_id,source_revision,delivery_type,source_created_at,
            nm_id,observed_at,collector_date_from,collector_date_to,collector_cursor
        ) VALUES('observation_late_revision',7001,'source_revision_late','fbs',?,101,?,1,2,0)""",
        ("2026-08-12T04:00:00Z", "2026-08-12T06:01:00Z"),
    )
    atomic.commit()
    late = classify_late_pre_t_observations(atomic, cutover_id="cutover_test_0001")
    assert late["count"] == 1 and all(item["creates_debit"] is False for item in late["cases"])
    assert late["cases"][0]["order_id"] == 7003
    repeated = _apply_ff_pool_cutover_fixture(atomic, proposal=atomic_proposal, deployed_sha=SHA, cutover_at=T)
    assert repeated["idempotent"] is True
    conflict = copy.deepcopy(atomic_proposal)
    conflict["allocations"][0]["capital_rub"] = "59.00"
    try:
        _apply_ff_pool_cutover_fixture(atomic, proposal=conflict, deployed_sha=SHA, cutover_at=T)
        raise AssertionError("different manifest accepted")
    except FfPoolCutoverError as exc:
        assert exc.code == "manifest_conflict"

    ambiguous = _db()
    ambiguous_proposal = _proposal(ambiguous)
    _hold(ambiguous, ambiguous_proposal)
    ambiguous.execute(f"CREATE TABLE {FIXTURE_MARKER_TABLE}(marker INTEGER)")
    ambiguous.commit()
    try:
        _apply_ff_pool_cutover_fixture(
            ambiguous, proposal=ambiguous_proposal, deployed_sha=SHA, cutover_at=T, crash="after_commit"
        )
        raise AssertionError("after-commit ambiguity not injected")
    except FfPoolCutoverAmbiguousCommit:
        pass
    assert read_ff_pool_cutover_status(ambiguous)["next_action"] == "exact_readback_and_human_reconciliation_required"
    for phase in ("recovery_required", "recovery_applying"):
        ambiguous.execute(
            f"INSERT INTO {EVENTS_TABLE}(epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (ambiguous_proposal["write_epoch_id"], phase, ambiguous_proposal["control_manifest_digest"], SHA, T, "smoke", "{}"),
        )
    ambiguous.execute(
        "INSERT INTO sheet_vitrina_v1_ff_stock_operations(operation_id) "
        "VALUES('unexpected_recovery_write')"
    )
    ambiguous.execute(
        f"INSERT INTO {EVENTS_TABLE}(epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            ambiguous_proposal["write_epoch_id"], "recovery_readback_required",
            ambiguous_proposal["control_manifest_digest"], SHA, T, "smoke", "{}",
        ),
    )
    ambiguous.commit()
    mismatch = read_ff_pool_cutover_readback(ambiguous, cutover_id="cutover_test_0001")
    assert mismatch["status"] == "mismatch" and "non_target_drift" in mismatch["mismatches"]

    plain = _db()
    plain_proposal = _proposal(plain)
    _hold(plain, plain_proposal)
    try:
        _apply_ff_pool_cutover_fixture(plain, proposal=plain_proposal, deployed_sha=SHA, cutover_at=T)
        raise AssertionError("normal DB gained apply surface")
    except FfPoolCutoverError as exc:
        assert exc.code == "fixture_marker_required"
    assert "apply" not in __import__("apps.ff_pool_cutover", fromlist=["main"]).__dict__
    assert all("raw" not in json.dumps(plan).lower() for _ in [0])
    assert set(WAREHOUSE_DOMAIN_TABLES).isdisjoint(
        {"sheet_vitrina_v1_wb_supplies", "sheet_vitrina_v1_wb_supplies_fbs_order_observations"}
    )
    fbw = _db()
    fbw.execute(
        "INSERT INTO sheet_vitrina_v1_wb_supplies VALUES(?,?,?,?,?,?,?,?)",
        ("supply_9001", "cache_9001", "9001", 2, "l", "d", "g", "p"),
    )
    fbw.commit()
    fbw_proposal = _proposal(fbw)
    _hold(fbw, fbw_proposal)
    missing_origin = build_ff_pool_cutover_plan(fbw, proposal=fbw_proposal, deployed_sha=SHA, cutover_at=T)
    assert any(item["code"] == "active_fbw_origin_unassigned" for item in missing_origin["blockers"])
    source = ff_pool_cutover_preflight_snapshot(fbw)["active_fbw_supplies"][0]
    fbw_proposal["fbw_origin_assignments"] = [{
        **source, "facility_id": "facility_test", "pool": "FBO", "evidence_digest": DIGEST,
    }]
    exact_origin = build_ff_pool_cutover_plan(fbw, proposal=fbw_proposal, deployed_sha=SHA, cutover_at=T)
    assert exact_origin["status"] == "ready", exact_origin["blockers"]

    signed = _db()
    signed.execute(
        "INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES(?,?,?,?,?,?)",
        ("wf_test", "ff", 102, "-2", "10.0025", "-20.005"),
    )
    signed.commit()
    signed_proposal = _proposal(signed)
    signed_proposal["allocations"].append(
        {"facility_id": "facility_test", "pool": "FBS", "nm_id": 102, "quantity": -2, "capital_rub": "-20.005"}
    )
    _hold(signed, signed_proposal)
    signed_plan = build_ff_pool_cutover_plan(signed, proposal=signed_proposal, deployed_sha=SHA, cutover_at=T)
    assert signed_plan["status"] == "ready", signed_plan["blockers"]
    assert signed_plan["apply_allowed"] is True
    assert signed_plan["production_apply_contract_ready"] is True
    zero_capital = copy.deepcopy(proposal)
    zero_capital["allocations"][0]["capital_rub"] = "0"
    zero_capital["allocations"][1]["capital_rub"] = "100"
    zero_capital_plan = build_ff_pool_cutover_plan(
        conn, proposal=zero_capital, deployed_sha=SHA, cutover_at=T
    )
    assert zero_capital_plan["status"] == "ready" and zero_capital_plan["apply_allowed"] is True
    zero = copy.deepcopy(signed_proposal)
    zero["allocations"].append(
        {"facility_id": "facility_test", "pool": "FBO", "nm_id": 999, "quantity": 0, "capital_rub": "0"}
    )
    try:
        build_ff_pool_cutover_plan(signed, proposal=zero, deployed_sha=SHA, cutover_at=T)
        raise AssertionError("zero allocation accepted")
    except FfPoolCutoverError as exc:
        assert exc.code == "zero_allocation_not_materialized"
    print("ff_pool_cutover_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
