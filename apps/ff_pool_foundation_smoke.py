"""Targeted checks for the inert FF facility/pool foundation."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    PARITY_TABLE,
    RELATIONS_TABLE,
    ensure_ff_pool_foundation_schema,
    evaluate_ff_pool_aggregate_parity,
    read_ff_pool_feature_state,
    record_ff_pool_parity_diagnostic,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (  # noqa: E402
    STAGES,
    ensure_warehouse_functional_schema,
)


NOW = "2026-08-11T09:00:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="ff-pool-foundation-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        assert runtime.list_ff_stock_operations(limit=1) == []
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_ff_pool_foundation_schema(conn)
            _assert_schema_is_empty_and_idempotent(conn)
            _assert_exact_storage_and_append_only_contract(conn)
            _assert_typed_forward_acyclic_relations(conn)
            _assert_feature_and_parity_contract(conn)
            _assert_bounded_query_plans(conn)
            conn.commit()

    assert STAGES == (
        "production",
        "china_to_ff",
        "ff",
        "ff_to_wb",
        "wb",
        "wb_acceptance_discrepancy",
    )
    print("ff_pool_foundation_smoke: OK")


def _assert_schema_is_empty_and_idempotent(conn: sqlite3.Connection) -> None:
    expected = {
        FACILITIES_TABLE,
        OPERATIONS_TABLE,
        LINES_TABLE,
        RELATIONS_TABLE,
        FEATURE_EPOCHS_TABLE,
        BALANCES_TABLE,
        PARITY_TABLE,
    }
    actual = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert expected.issubset(actual)
    before = _foundation_counts(conn)
    assert before == {table: 0 for table in expected}
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    ensure_ff_pool_foundation_schema(conn)
    assert _foundation_counts(conn) == before
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]) == journal_mode


def _assert_exact_storage_and_append_only_contract(conn: sqlite3.Connection) -> None:
    _insert_facility(conn, "facility-a", "TEST-A")
    _insert_operation(conn, "movement-a", "pool_movement", "08:00:00")
    conn.execute(
        f"""INSERT INTO {LINES_TABLE}(
                operation_id,line_no,facility_id,pool,nm_id,quantity_delta,
                capital_delta_rub,wac_snapshot_rub,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("movement-a", 1, "facility-a", "FBS", 101, 2, "20.1250", "10.0625", "{}"),
    )
    stored = conn.execute(
        f"""SELECT typeof(quantity_delta),typeof(capital_delta_rub),
                   capital_delta_rub,wac_snapshot_rub
            FROM {LINES_TABLE} WHERE operation_id='movement-a' AND line_no=1"""
    ).fetchone()
    assert stored == ("integer", "text", "20.1250", "10.0625")

    _assert_rejected(
        lambda: conn.execute(
            f"""INSERT INTO {LINES_TABLE} VALUES(
                'movement-a',2,'facility-a','UNALLOCATED',101,1,'10',NULL,'{{}}')"""
        ),
        "pool enum",
    )
    _assert_rejected(
        lambda: conn.execute(
            f"""INSERT INTO {LINES_TABLE} VALUES(
                'movement-a',3,'facility-a','FBO',101,1.5,'15',NULL,'{{}}')"""
        ),
        "fractional quantity",
    )
    _assert_rejected(
        lambda: conn.execute(
            f"""INSERT INTO {LINES_TABLE} VALUES(
                'movement-a',4,'facility-a','FBO',101,1,'1e2',NULL,'{{}}')"""
        ),
        "exponent capital",
    )
    _assert_rejected(
        lambda: _insert_operation(
            conn,
            "movement-duplicate-source",
            "pool_movement",
            "08:01:00",
            source_id="movement-a",
        ),
        "source identity uniqueness",
    )
    _assert_rejected(
        lambda: conn.execute(
            f"UPDATE {OPERATIONS_TABLE} SET operation_type='changed' WHERE operation_id='movement-a'"
        ),
        "posted header update",
    )
    _assert_rejected(
        lambda: conn.execute(
            f"DELETE FROM {LINES_TABLE} WHERE operation_id='movement-a'"
        ),
        "posted line delete",
    )


def _assert_typed_forward_acyclic_relations(conn: sqlite3.Connection) -> None:
    _insert_operation(conn, "original", "pool_movement", "09:00:00")
    _insert_operation(conn, "correction", "correction", "09:01:00")
    conn.execute(
        f"INSERT INTO {RELATIONS_TABLE} VALUES(?,?,?,?)",
        ("original", "correction", "correction_of", NOW),
    )
    _assert_rejected(
        lambda: conn.execute(
            f"INSERT INTO {RELATIONS_TABLE} VALUES(?,?,?,?)",
            ("original", "correction", "correction_of", NOW),
        ),
        "relation uniqueness",
    )

    _insert_operation(conn, "wrong-child", "pool_movement", "09:02:00")
    _assert_rejected(
        lambda: conn.execute(
            f"INSERT INTO {RELATIONS_TABLE} VALUES(?,?,?,?)",
            ("original", "wrong-child", "storno_of", NOW),
        ),
        "typed relation child",
    )
    _insert_operation(conn, "late-parent", "pool_movement", "10:00:00")
    _insert_operation(conn, "early-child", "correction", "09:59:00")
    _assert_rejected(
        lambda: conn.execute(
            f"INSERT INTO {RELATIONS_TABLE} VALUES(?,?,?,?)",
            ("late-parent", "early-child", "correction_of", NOW),
        ),
        "forward chronology",
    )

    for operation_id in ("cycle-a", "cycle-b", "cycle-c"):
        _insert_operation(conn, operation_id, "correction", "11:00:00")
    conn.execute(
        f"INSERT INTO {RELATIONS_TABLE} VALUES('cycle-a','cycle-b','correction_of',?)",
        (NOW,),
    )
    conn.execute(
        f"INSERT INTO {RELATIONS_TABLE} VALUES('cycle-b','cycle-c','correction_of',?)",
        (NOW,),
    )
    _assert_rejected(
        lambda: conn.execute(
            f"INSERT INTO {RELATIONS_TABLE} VALUES('cycle-c','cycle-a','correction_of',?)",
            (NOW,),
        ),
        "relation cycle",
    )
    _assert_rejected(
        lambda: conn.execute(
            f"DELETE FROM {RELATIONS_TABLE} WHERE child_id='correction'"
        ),
        "relation delete",
    )


def _assert_feature_and_parity_contract(conn: sqlite3.Connection) -> None:
    aggregate = [
        {
            "nm_id": 101,
            "quantity": 10,
            "capital_rub": "319434.32291654259178871196266",
        },
        {"nm_id": 202, "quantity": 3, "capital_rub": "37.50"},
    ]
    off = evaluate_ff_pool_aggregate_parity(conn, aggregate)
    assert off.status == "feature_off" and not off.fail_closed
    feature_off = read_ff_pool_feature_state(conn)
    assert feature_off.epoch == 0 and not feature_off.writer_effective
    _assert_rejected(
        lambda: conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,0,1,'invalid',?,'{{}}')",
            (NOW,),
        ),
        "reader cannot precede writer",
    )
    conn.execute(
        f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,0,0,'fixture-epoch-off',?,'{{}}')",
        (NOW,),
    )
    conn.execute(
        f"""INSERT INTO {BALANCES_TABLE}(
                facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                wac_rub,source_watermark,updated_at
            ) VALUES('facility-a','FBS',999,1,100,'1000','10','off-wm',?)""",
        (NOW,),
    )
    populated_off = evaluate_ff_pool_aggregate_parity(conn, aggregate)
    assert populated_off.status == "feature_off" and populated_off.feature_epoch == 1
    conn.execute(
        f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(2,1,1,'fixture-epoch-2',?,'{{}}')",
        (NOW,),
    )
    empty = evaluate_ff_pool_aggregate_parity(conn, aggregate)
    assert empty.status == "detail_empty" and not empty.fail_closed
    assert not read_ff_pool_feature_state(conn).reader_effective

    _insert_facility(conn, "facility-b", "TEST-B")
    balance_rows = (
        (
            "facility-a",
            "FBS",
            101,
            2,
            4,
            "22685.48291654259178871196266",
            "10",
            "wm-1",
            NOW,
        ),
        ("facility-b", "FBO", 101, 2, 6, "296748.84", "10.0", "wm-1", NOW),
        ("facility-a", "FBO", 202, 2, 3, "37.50", "12.50", "wm-1", NOW),
    )
    conn.executemany(
        f"""INSERT INTO {BALANCES_TABLE}(
                facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                wac_rub,source_watermark,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
        balance_rows,
    )
    passed = evaluate_ff_pool_aggregate_parity(conn, aggregate)
    assert passed.status == "pass"
    assert passed.detail_quantity == passed.aggregate_quantity == 13
    assert passed.detail_capital_rub == passed.aggregate_capital_rub
    assert passed.detail_fingerprint.startswith("sha256:")
    assert passed.aggregate_fingerprint.startswith("sha256:")
    assert passed.reader_allowed and passed.aggregate_unchanged
    rounded_aggregate = deepcopy(aggregate)
    rounded_aggregate[0]["capital_rub"] = "319434.3229165425917887119627"
    precision_diagnostic = evaluate_ff_pool_aggregate_parity(
        conn, rounded_aggregate
    )
    assert precision_diagnostic.status == "pass"
    assert precision_diagnostic.mismatched_nm_ids == ()
    assert precision_diagnostic.raw_capital_mismatched_nm_ids == (101,)
    assert precision_diagnostic.canonical_capital_mismatched_nm_ids == ()
    assert precision_diagnostic.detail_canonical_capital_minor_units == (
        precision_diagnostic.aggregate_canonical_capital_minor_units
    )
    assert precision_diagnostic.raw_residual_conserved
    assert not precision_diagnostic.fail_closed

    canonical_mismatch_aggregate = deepcopy(aggregate)
    canonical_mismatch_aggregate[0]["capital_rub"] = str(
        Decimal(str(aggregate[0]["capital_rub"])) + Decimal("0.01")
    )
    canonical_mismatch = evaluate_ff_pool_aggregate_parity(
        conn, canonical_mismatch_aggregate
    )
    assert canonical_mismatch.status == "mismatch"
    assert canonical_mismatch.canonical_capital_mismatched_nm_ids == (101,)
    assert canonical_mismatch.mismatched_nm_ids == (101,)

    # Individually sub-kopeck residuals may still accumulate across SKUs.  If
    # their exact total crosses a canonical kopeck boundary, conservation stays
    # fail-closed even though each row alone remains in the same kopeck bucket.
    accumulated_residual = deepcopy(aggregate)
    accumulated_residual[0]["capital_rub"] = str(
        Decimal(str(aggregate[0]["capital_rub"])) - Decimal("0.004")
    )
    accumulated_residual[1]["capital_rub"] = str(
        Decimal(str(aggregate[1]["capital_rub"])) - Decimal("0.004")
    )
    accumulated_mismatch = evaluate_ff_pool_aggregate_parity(
        conn, accumulated_residual
    )
    assert accumulated_mismatch.status == "mismatch"
    assert accumulated_mismatch.canonical_capital_mismatched_nm_ids == ()
    assert accumulated_mismatch.raw_capital_mismatched_nm_ids == (101, 202)
    assert accumulated_mismatch.detail_canonical_capital_minor_units != (
        accumulated_mismatch.aggregate_canonical_capital_minor_units
    )
    assert accumulated_mismatch.raw_residual_conserved

    fractional_quantity = deepcopy(aggregate)
    fractional_quantity[0]["quantity"] = "10.5"
    _assert_rejected_value(
        lambda: evaluate_ff_pool_aggregate_parity(conn, fractional_quantity),
        "fractional aggregate quantity",
    )
    record_ff_pool_parity_diagnostic(
        conn,
        diagnostic_id="parity-pass",
        aggregate_revision="aggregate-fixture-v1",
        checked_at="2026-08-11T09:01:00Z",
        result=precision_diagnostic,
    )
    parity_details = conn.execute(
        f"SELECT details_json FROM {PARITY_TABLE} WHERE diagnostic_id='parity-pass'"
    ).fetchone()
    assert parity_details is not None
    stored_details = json.loads(str(parity_details[0]))
    assert stored_details["money_parity_policy"] == "rub_minor_unit_round_half_up_v1"
    assert stored_details["raw_capital_mismatched_nm_ids"] == [101]
    assert stored_details["raw_capital_residuals_by_nm"] == {
        "101": "-0.00000000000000000000004"
    }
    assert stored_details["raw_residual_conserved"] is True
    assert not read_ff_pool_feature_state(conn).reader_effective
    state = read_ff_pool_feature_state(
        conn,
        aggregate_revision="aggregate-fixture-v1",
    )
    assert state.reader_effective and state.parity_status == "pass"
    revision_drift = read_ff_pool_feature_state(
        conn,
        aggregate_revision="aggregate-fixture-other",
    )
    assert not revision_drift.reader_effective
    assert revision_drift.reason == "current_aggregate_revision_drift_fail_closed"
    conn.execute(
        f"UPDATE {BALANCES_TABLE} SET source_watermark='wm-2' WHERE facility_id='facility-a' AND pool='FBS' AND nm_id=101"
    )
    _assert_rejected_value(
        lambda: record_ff_pool_parity_diagnostic(
            conn,
            diagnostic_id="stale-parity-pass",
            aggregate_revision="aggregate-fixture-v1",
            checked_at="2026-08-11T09:01:30Z",
            result=passed,
        ),
        "stale detail parity record",
    )
    drifted_state = read_ff_pool_feature_state(
        conn,
        aggregate_revision="aggregate-fixture-v1",
    )
    assert not drifted_state.reader_effective
    assert drifted_state.reason == "current_detail_projection_drift_fail_closed"

    ensure_warehouse_functional_schema(conn)
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
            version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
            cost_covered_quantity,quality,certified,wb_quantity,
            wb_in_way_to_client,wb_in_way_from_client,provenance_json
        ) VALUES('legacy-global','ff',101,'10','10','100','10','certified',1,'0','0','0','{}')"""
    )
    before = conn.execute(
        """SELECT quantity,wac_rub,capital_rub
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id='legacy-global' AND warehouse_key='ff' AND nm_id=101"""
    ).fetchone()
    mismatch_aggregate = deepcopy(aggregate)
    mismatch_aggregate[0] = {"nm_id": 101, "quantity": 9, "capital_rub": "99"}
    mismatch = evaluate_ff_pool_aggregate_parity(conn, mismatch_aggregate)
    after = conn.execute(
        """SELECT quantity,wac_rub,capital_rub
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id='legacy-global' AND warehouse_key='ff' AND nm_id=101"""
    ).fetchone()
    assert mismatch.status == "mismatch" and mismatch.fail_closed
    assert not mismatch.reader_allowed and mismatch.aggregate_unchanged
    assert mismatch.mismatched_nm_ids == (101,)
    assert before == after == ("10", "10", "100")
    assert mismatch_aggregate[0] == {"nm_id": 101, "quantity": 9, "capital_rub": "99"}
    record_ff_pool_parity_diagnostic(
        conn,
        diagnostic_id="parity-mismatch",
        aggregate_revision="aggregate-fixture-v2",
        checked_at="2026-08-11T09:02:00Z",
        result=mismatch,
    )
    mismatch_state = read_ff_pool_feature_state(
        conn,
        aggregate_revision="aggregate-fixture-v2",
    )
    assert not mismatch_state.reader_effective
    assert mismatch_state.reason == "current_epoch_parity_mismatch_fail_closed"
    assert conn.execute(f"SELECT COUNT(*) FROM {PARITY_TABLE}").fetchone()[0] == 2


def _assert_bounded_query_plans(conn: sqlite3.Connection) -> None:
    balance_plan = " ".join(
        str(part)
        for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT quantity,capital_rub FROM {BALANCES_TABLE} WHERE pool='FBS' AND nm_id=101"
        ).fetchall()
        for part in row
    )
    assert "ff_pool_balances_by_pool_nm" in balance_plan
    movement_plan = " ".join(
        str(part)
        for row in conn.execute(
            f"""EXPLAIN QUERY PLAN
                SELECT quantity_delta,capital_delta_rub FROM {LINES_TABLE}
                WHERE facility_id='facility-a' AND pool='FBS' AND nm_id=101"""
        ).fetchall()
        for part in row
    )
    assert "ff_pool_movement_lines_by_" in movement_plan


def _insert_facility(conn: sqlite3.Connection, facility_id: str, code: str) -> None:
    conn.execute(
        f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,?,?,?)",
        (facility_id, code, f"Fixture {code}", 1, "Asia/Yekaterinburg", NOW, NOW),
    )


def _insert_operation(
    conn: sqlite3.Connection,
    operation_id: str,
    operation_type: str,
    time_text: str,
    *,
    source_id: str | None = None,
) -> None:
    conn.execute(
        f"""INSERT INTO {OPERATIONS_TABLE}(
            operation_id,operation_type,source_system,source_type,source_id,
            source_revision,idempotency_epoch,business_date,posted_at,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            operation_type,
            "fixture",
            "smoke",
            source_id or operation_id,
            "revision-1",
            1,
            "2026-08-11",
            f"2026-08-11T{time_text}Z",
            "{}",
        ),
    )


def _foundation_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in {
            FACILITIES_TABLE,
            OPERATIONS_TABLE,
            LINES_TABLE,
            RELATIONS_TABLE,
            FEATURE_EPOCHS_TABLE,
            BALANCES_TABLE,
            PARITY_TABLE,
        }
    }


def _assert_rejected(action: Callable[[], object], label: str) -> None:
    try:
        action()
    except sqlite3.DatabaseError:
        return
    raise AssertionError(f"{label} must fail closed")


def _assert_rejected_value(action: Callable[[], object], label: str) -> None:
    try:
        action()
    except ValueError:
        return
    raise AssertionError(f"{label} must fail closed")


if __name__ == "__main__":
    main()
