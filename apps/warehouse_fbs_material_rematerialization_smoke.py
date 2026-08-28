from __future__ import annotations

from decimal import Decimal, localcontext
import errno
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import CalculationParametersBlock
from packages.application.calculation_parameters_v4 import (
    PROXY_V4_FORMULA_VERSION,
    ProxyV4Parameters,
    ensure_proxy_v4_schema,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    ensure_ff_pool_foundation_schema,
    canonical_decimal_ratio_text,
    canonical_decimal_text,
)
from packages.application.ff_pool_fbs_lifecycle import (
    _apply_exact_physical_delta,
    _enqueue_lifecycle_material_recalculation,
)
from packages.application.inventory_cost_blend import build_inventory_cost_blend_lookup
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
)
from packages.application.warehouse_fbs_material_rematerialization import (
    CRITICAL_TOTAL_METRIC_KEYS,
    HISTORICAL_RECOVERY_REQUIRED,
    HISTORICAL_MANIFEST_SCHEMA,
    MAX_PERSISTED_PLAN_BYTES,
    MAX_READY_CLOSURE_BYTES,
    REPAIRABLE,
    REPAIRED,
    RETRY_EXHAUSTED,
    UNSAFE_AMBIGUOUS,
    WarehouseFbsMaterialRematerializer,
    _fingerprint,
    _functional_pool_mismatches,
    _historical_event_aggregate,
    _pool_aggregates,
    _warehouse_metric_lookup,
    ensure_warehouse_fbs_material_schema,
    publish_fbs_pool_aggregate_revision,
)
from packages.application.warehouse_functional_economics_backfill import (
    _transform_snapshot,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from apps.wbc0013_fbs_recovery import _discover_historical_manifests


DAY = "2026-08-26"
LEGACY_WAC_161 = "10." + "0" * 158
NOW = "2026-08-26T12:00:00Z"
FACILITY_ID = "fac_incident"
MOSCOW_FACILITY_ID = "fac_moscow_preserved"
TARGET_NM_ID = 101
NON_TARGET_NM_ID = 202
WAC_CURRENT_ONLY_NM_ID = 8_028
STAGES = (
    "production",
    "china_to_ff",
    "ff",
    "ff_to_wb",
    "wb",
    "wb_acceptance_discrepancy",
)


class _Clock:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self) -> str:
        self.counter += 1
        return f"2026-08-26T12:00:{self.counter:02d}Z"


def main() -> None:
    _test_shared_wac_precision_contract()
    _test_historical_precision38_event_arithmetic()
    _test_atomic_root_prevention()
    _test_lifecycle_canonical_zero_shape()
    _test_incident_plan_apply_idempotency_and_bounds()
    _test_historical_bounded_recovery_preserves_current()
    _test_resume_before_and_after_commit_transport_loss()
    _test_drift_concurrency_and_fail_closed_boundaries()
    print("warehouse_fbs_material_atomic_1953_to_1952: OK")
    print("warehouse_fbs_material_wac_28_classification: OK")
    print("warehouse_fbs_material_single_sku_repair_resume: OK")
    print("warehouse_fbs_material_historical_bounded_recovery: OK")
    print("warehouse_fbs_material_cas_bounds_benchmark: OK")


def _test_historical_precision38_event_arithmetic() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(
                   version_id TEXT,warehouse_key TEXT,nm_id INTEGER,quantity TEXT,
                   capital_rub TEXT,wac_rub TEXT,cost_covered_quantity TEXT,
                   provenance_json TEXT)"""
        )
        quantity_delta = Decimal("-1234567890")
        frozen_wac = Decimal("0.12345678901234567890123456789")
        with localcontext() as arithmetic:
            arithmetic.prec = 38
            capital_delta = +(quantity_delta * frozen_wac)
            prior_quantity = Decimal("1") - quantity_delta
            prior_capital = frozen_wac - capital_delta
        provenance = {
            "source_records": [
                {
                    "locations": [
                        {
                            "facility_id": FACILITY_ID,
                            "pool": "FBS",
                            "quantity": canonical_decimal_text(prior_quantity),
                            "capital_rub": canonical_decimal_text(prior_capital),
                        }
                    ]
                }
            ]
        }
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_balances "
            "VALUES('precision38','ff',101,'1',?,?, '2',?)",
            (
                canonical_decimal_text(frozen_wac),
                canonical_decimal_text(frozen_wac),
                json.dumps(provenance, sort_keys=True),
            ),
        )
        aggregate = _historical_event_aggregate(
            conn,
            source_version_id="precision38",
            facility_id=FACILITY_ID,
            nm_id=TARGET_NM_ID,
            event={
                "event_id": "precision38-event",
                "physical_quantity_delta": canonical_decimal_text(quantity_delta),
                "capital_delta_rub": canonical_decimal_text(capital_delta),
                "frozen_wac_rub": canonical_decimal_text(frozen_wac),
            },
        )
        assert aggregate["quantity"] == "1"
        assert aggregate["capital_rub"] == canonical_decimal_text(frozen_wac)
        assert aggregate["locations"][0]["quantity"] == "1"


def _build_historical_plan(
    service: WarehouseFbsMaterialRematerializer,
    runtime: RegistryUploadDbBackedRuntime,
    manifest: dict[str, object],
) -> dict[str, object]:
    uri = f"file:{runtime.db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as dependency_conn:
        dependency_conn.row_factory = sqlite3.Row
        dependency_conn.execute("PRAGMA query_only=ON")
        statements: list[str] = []
        dependency_conn.set_trace_callback(statements.append)
        result = service.build_historical_plan(
            manifest,
            dependency_conn=dependency_conn,
        )
        assert not any(
            statement.lstrip().upper().startswith(
                (
                    "CREATE ",
                    "ALTER ",
                    "DROP ",
                    "INSERT ",
                    "UPDATE ",
                    "DELETE ",
                    "REPLACE ",
                    "BEGIN ",
                    "COMMIT",
                )
            )
            for statement in statements
        ), statements
        return result


def _test_shared_wac_precision_contract() -> None:
    prime_quantities = (
        101,
        103,
        107,
        109,
        113,
        127,
        131,
        137,
        139,
        149,
        151,
        157,
        163,
        167,
        173,
        179,
        181,
        191,
        193,
        197,
        199,
        211,
        223,
        227,
        229,
        233,
    )
    with tempfile.TemporaryDirectory(prefix="fbs-material-wac-28-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=False)
        precision_nm_ids: list[int] = []
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for position, raw_quantity in enumerate(prime_quantities, start=1):
                nm_id = 8_000 + position
                precision_nm_ids.append(nm_id)
                quantity = Decimal(raw_quantity)
                capital = Decimal("100000.12345678901234567890123456789") + position
                stored = canonical_decimal_ratio_text(capital, quantity)
                with localcontext() as context:
                    context.prec = 160
                    broad_reader_value = canonical_decimal_text(capital / quantity)
                assert stored != broad_reader_value
                conn.execute(
                    f"""INSERT INTO {BALANCES_TABLE}(
                           facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                           wac_rub,source_watermark,updated_at)
                       VALUES(?,'FBS',?,1,?,?,?,?,?)""",
                    (
                        FACILITY_ID,
                        nm_id,
                        canonical_decimal_text(quantity),
                        canonical_decimal_text(capital),
                        stored,
                        f"wac-precision-{position}",
                        NOW,
                    ),
                )
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                           version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                           cost_covered_quantity,quality,certified,wb_quantity,
                           wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                       VALUES('whfv_incident_source','ff',?,?,?,?,?,'exact',1,'0','0','0','{}')""",
                    (
                        nm_id,
                        canonical_decimal_text(quantity),
                        stored,
                        canonical_decimal_text(capital),
                        canonical_decimal_text(quantity),
                    ),
                )
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at)
                   VALUES(?,'FBS',?,1,1,'10','10','current-only',?)""",
                (FACILITY_ID, WAC_CURRENT_ONLY_NM_ID, NOW),
            )
            conn.commit()
            aggregates = _pool_aggregates(conn, precision_nm_ids)
            assert sorted(aggregates) == precision_nm_ids
            assert _functional_pool_mismatches(conn, "whfv_incident_source") == [
                WAC_CURRENT_ONLY_NM_ID
            ]
        assert len(precision_nm_ids) == 26


def _test_atomic_root_prevention() -> None:
    with tempfile.TemporaryDirectory(prefix="fbs-material-atomic-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=False)
        before = _fingerprints(runtime.db_path)
        writer = sqlite3.connect(runtime.db_path, timeout=30)
        writer.row_factory = sqlite3.Row
        observer = sqlite3.connect(runtime.db_path, timeout=30)
        observer.row_factory = sqlite3.Row
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                f"""UPDATE {BALANCES_TABLE}
                    SET quantity=1952,capital_rub='19520',wac_rub='10',
                        source_watermark='handoff-debit-1',updated_at=?
                    WHERE facility_id=? AND pool='FBS' AND nm_id=?""",
                ("2026-08-26T12:01:00Z", FACILITY_ID, TARGET_NM_ID),
            )
            result = publish_fbs_pool_aggregate_revision(
                writer,
                affected_nm_ids=[TARGET_NM_ID],
                source_kind="fbs_order_lifecycle_event",
                source_id="handoff-debit-1",
                business_date=DAY,
                published_at="2026-08-26T12:01:00Z",
            )
            _enqueue_lifecycle_material_recalculation(
                writer,
                event_id="handoff-debit-1",
                nm_id=TARGET_NM_ID,
                business_date=DAY,
                requested_at="2026-08-26T12:01:00Z",
                target_version_id=str(result["target_version_id"]),
            )
            # A concurrent reader sees the complete accepted 1953 image while
            # the 1952 candidate and projection are uncommitted.
            visible = _active_ff(observer, TARGET_NM_ID)
            assert visible[0:2] == ("1953", "1953"), visible
            assert (
                observer.execute(
                    f"SELECT quantity FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?",
                    (FACILITY_ID, TARGET_NM_ID),
                ).fetchone()[0]
                == 1953
            )
            writer.commit()
        finally:
            writer.close()
            observer.close()
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            after = _active_ff(conn, TARGET_NM_ID)
            assert after[0:4] == ("1952", "1952", "19520", "10"), after
            assert (
                conn.execute(
                    f"SELECT quantity FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?",
                    (FACILITY_ID, TARGET_NM_ID),
                ).fetchone()[0]
                == 1952
            )
            assert (
                conn.execute(
                    "SELECT status FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
                    (result["target_version_id"],),
                ).fetchone()[0]
                == "good"
            )
            assert (
                conn.execute(
                    "SELECT status FROM sheet_vitrina_v1_warehouse_business_projection_revisions "
                    "WHERE published_version_id=?",
                    (result["target_version_id"],),
                ).fetchone()[0]
                == "active"
            )
            queue = conn.execute(
                """SELECT status,affected_nm_ids_json
                   FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                   WHERE stable_source_id='fbs_lifecycle:handoff-debit-1'"""
            ).fetchone()
            assert tuple(queue) == ("queued", f"[{TARGET_NM_ID}]")
        after_fingerprints = _fingerprints(runtime.db_path)
        assert before["non_target"] == after_fingerprints["non_target"]
        assert before["reservations"] == after_fingerprints["reservations"]
        assert before["orders"] == after_fingerprints["orders"]
        assert before["source_history"] == after_fingerprints["source_history"]
        assert (
            before["ready_non_target_sentinel"]
            == after_fingerprints["ready_non_target_sentinel"]
        )


def _test_lifecycle_canonical_zero_shape() -> None:
    with tempfile.TemporaryDirectory(prefix="fbs-material-zero-shape-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=False)
        occurred_at = "2026-08-26T12:02:00Z"
        event_id = "handoff-debit-to-zero"
        with warehouse_functional_write_lock(runtime.runtime_dir):
            with sqlite3.connect(runtime.db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_events(
                           event_id,cutover_id,order_id,episode_sequence,event_type,
                           source_order_observation_sequence,
                           source_status_observation_sequence,source_revision,
                           status_digest,supplier_status,wb_status,source_observed_at,
                           facility_id,pool,nm_id,quantity,physical_quantity_delta,
                           capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at)
                       VALUES(?,?,?,1,'handoff_debit',1,1,?,'status-zero',
                              'complete','sorted',?,?,'FBS',?,1953,-1953,
                              '-19530','10','evidence-zero',?)""",
                    (
                        event_id,
                        "warehouse_functional_cutover_v1",
                        9003,
                        "revision-zero",
                        occurred_at,
                        FACILITY_ID,
                        TARGET_NM_ID,
                        occurred_at,
                    ),
                )
                _apply_exact_physical_delta(
                    conn,
                    manifest={"feature_epoch": 1, "business_date": DAY},
                    order={
                        "order_id": 9003,
                        "facility_id": FACILITY_ID,
                        "nm_id": TARGET_NM_ID,
                    },
                    event_id=event_id,
                    quantity_delta=-1953,
                    wac=Decimal("10"),
                    occurred_at=occurred_at,
                )
                conn.commit()
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            physical = conn.execute(
                f"""SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE}
                    WHERE facility_id=? AND pool='FBS' AND nm_id=?""",
                (FACILITY_ID, TARGET_NM_ID),
            ).fetchone()
            assert tuple(physical) == (0, "0", None), tuple(physical)
            assert _active_ff(conn, TARGET_NM_ID) == ("0", "0", "0", None)
            assert (
                conn.execute(
                    """SELECT status FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                   WHERE stable_source_id=?""",
                    (f"fbs_lifecycle:{event_id}",),
                ).fetchone()[0]
                == "queued"
            )


def _test_historical_bounded_recovery_preserves_current() -> None:
    with tempfile.TemporaryDirectory(prefix="fbs-material-historical-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        current_version = "whfv_current_day_preserved"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            source = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions "
                "WHERE version_id='whfv_incident_source'"
            ).fetchone()
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                       version_id,cutover_id,version_kind,effective_at,
                       business_effective_date,published_at,status,plan_fingerprint,
                       local_source_digest,source_watermarks_json,created_at)
                   VALUES(?,?,?,?,?,?,'good',?,?,?,?)""",
                (
                    current_version,
                    str(source["cutover_id"]),
                    "hourly_wb_sync",
                    "2026-08-27T12:00:00Z",
                    "2026-08-27",
                    "2026-08-27T12:00:00Z",
                    "sha256:current-preserved",
                    "sha256:current-local",
                    "{}",
                    "2026-08-27T12:00:00Z",
                ),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                       cost_covered_quantity,quality,certified,wb_quantity,
                       wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                   SELECT ?,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                          cost_covered_quantity,quality,certified,wb_quantity,
                          wb_in_way_to_client,wb_in_way_from_client,provenance_json
                     FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id='whfv_incident_source'""",
                (current_version,),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_active "
                "SET version_id=?,updated_at=? WHERE slot=1",
                (current_version, "2026-08-27T12:00:00Z"),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_wb_sync_status "
                "SET active_version_id=?,updated_at=? WHERE slot=1",
                (current_version, "2026-08-27T12:00:00Z"),
            )
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at)
                   VALUES(?,'FBS',303,1,0,'0',NULL,'dense-a-zero',?)""",
                (FACILITY_ID, "2026-08-27T12:00:00Z"),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                       bundle_version,activated_at,as_of_date,snapshot_id,
                       plan_version,refreshed_at,plan_json)
                   VALUES('other-date-bundle',?,'2026-08-25','other-date-ready',
                          'v1',?,?)""",
                (
                    NOW,
                    NOW,
                    json.dumps({"date_columns": ["2026-08-25"], "sentinel": 825}),
                ),
            )
            conn.executemany(
                """INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_events(
                       event_id,cutover_id,order_id,episode_sequence,event_type,
                       source_order_observation_sequence,
                       source_status_observation_sequence,source_revision,
                       status_digest,supplier_status,wb_status,source_observed_at,
                       facility_id,pool,nm_id,quantity,physical_quantity_delta,
                       capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at)
                   VALUES(?,'warehouse_functional_cutover_v1',?,1,'handoff_debit',
                          1,1,?,?,'complete','sorted',?,?,'FBS',?,1,-1,'-10',
                          '10',?,?)""",
                (
                    (
                        event_id,
                        order_id,
                        f"revision-{order_id}",
                        _fingerprint(f"status-{order_id}"),
                        occurred_at,
                        FACILITY_ID,
                        TARGET_NM_ID,
                        _fingerprint(f"evidence-{order_id}"),
                        occurred_at,
                    )
                    for event_id, order_id, occurred_at in (
                        ("handoff-pre-publication", 9003, "2026-08-26T11:59:59Z"),
                        ("handoff-next-boundary", 9004, "2026-08-27T12:00:00Z"),
                    )
                ),
            )
            broad_count = int(
                conn.execute(
                    "SELECT COUNT(*) "
                    "FROM sheet_vitrina_v1_warehouse_functional_versions version "
                    "JOIN sheet_vitrina_v1_warehouse_functional_balances balance "
                    "ON balance.version_id=version.version_id "
                    "AND balance.warehouse_key='ff' "
                    "WHERE version.status='good' AND balance.quantity>0 "
                    "AND balance.cost_covered_quantity<>balance.quantity"
                ).fetchone()[0]
            )
            assert broad_count < 160
            for number in range(160 - broad_count):
                stale_version = f"whfv_stale_local_reject_{number:03d}"
                stale_second = number % 60
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                           version_id,cutover_id,version_kind,effective_at,
                           business_effective_date,published_at,status,plan_fingerprint,
                           local_source_digest,source_watermarks_json,created_at)
                       VALUES(?,?,'hourly_wb_sync',?,'2025-01-01',?,'good',?,?,?,?)""",
                    (
                        stale_version,
                        str(source["cutover_id"]),
                        f"2025-01-01T00:{number // 60:02d}:{stale_second:02d}Z",
                        f"2025-01-01T00:{number // 60:02d}:{stale_second:02d}Z",
                        _fingerprint(f"stale-plan-{number}"),
                        _fingerprint(f"stale-local-{number}"),
                        "{}",
                        f"2025-01-01T00:{number // 60:02d}:{stale_second:02d}Z",
                    ),
                )
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                           version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                           cost_covered_quantity,quality,certified,wb_quantity,
                           wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                       SELECT ?,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                              cost_covered_quantity,quality,certified,wb_quantity,
                              wb_in_way_to_client,wb_in_way_from_client,provenance_json
                         FROM sheet_vitrina_v1_warehouse_functional_balances
                        WHERE version_id='whfv_incident_source' AND warehouse_key='ff'
                          AND nm_id=?""",
                    (stale_version, TARGET_NM_ID),
                )
            discovery = _discover_historical_manifests(
                conn,
                canonical_target={"accepted": True},
                storage_generation={"implicit": False, "query_only": True},
            )
            assert len(discovery["mismatches"]) == 160
            assert len(discovery["manifests"]) == 1
            assert discovery["manifests"][0]["event_id"] == "handoff-debit-1"
            assert any(
                item["classification"] == "rejected_ready_shape_prerequisites"
                for item in discovery["mismatches"]
            )
            assert discovery["selection"]["predicate"] == (
                "historical_b.exact_causal_handoff_debit_event"
            )
            assert discovery["selection"]["expected_cardinality"] == 1
            assert discovery["selection"]["observed_cardinality"] == 1
            assert len(
                [
                    item
                    for item in discovery["mismatches"]
                    if item["classification"]
                    == "rejected_ready_shape_prerequisites"
                ]
            ) == 159

            conn.execute("SAVEPOINT ready_zero")
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                (DAY,),
            )
            ready_zero = _discover_historical_manifests(
                conn,
                canonical_target={"accepted": True},
                storage_generation={"implicit": False, "query_only": True},
            )
            assert ready_zero["manifests"] == []
            assert ready_zero["selection"]["predicate"] == (
                "historical_b.exact_ready_shape_candidate"
            )
            assert ready_zero["selection"]["observed_cardinality"] == 0
            conn.execute("ROLLBACK TO ready_zero")
            conn.execute("RELEASE ready_zero")

            conn.execute("SAVEPOINT ready_many")
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                       version_id,cutover_id,version_kind,effective_at,
                       business_effective_date,published_at,status,plan_fingerprint,
                       local_source_digest,source_watermarks_json,created_at)
                   SELECT 'whfv_ready_shape_ambiguous',cutover_id,version_kind,
                          effective_at,business_effective_date,
                          '2026-08-26T11:59:00Z',status,
                          'sha256:ready-shape-ambiguous',local_source_digest,
                          source_watermarks_json,created_at
                     FROM sheet_vitrina_v1_warehouse_functional_versions
                    WHERE version_id='whfv_incident_source'"""
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                       cost_covered_quantity,quality,certified,wb_quantity,
                       wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                   SELECT 'whfv_ready_shape_ambiguous',warehouse_key,nm_id,quantity,
                          wac_rub,capital_rub,cost_covered_quantity,quality,certified,
                          wb_quantity,wb_in_way_to_client,wb_in_way_from_client,
                          provenance_json
                     FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id='whfv_incident_source'"""
            )
            ready_many = _discover_historical_manifests(
                conn,
                canonical_target={"accepted": True},
                storage_generation={"implicit": False, "query_only": True},
            )
            assert ready_many["manifests"] == []
            assert ready_many["selection"]["predicate"] == (
                "historical_b.exact_ready_shape_candidate"
            )
            assert ready_many["selection"]["observed_cardinality"] == 2
            conn.execute("ROLLBACK TO ready_many")
            conn.execute("RELEASE ready_many")

            conn.execute("SAVEPOINT causal_zero")
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                       version_id,cutover_id,version_kind,effective_at,
                       business_effective_date,published_at,status,plan_fingerprint,
                       local_source_digest,source_watermarks_json,created_at)
                   SELECT 'whfv_causal_zero',cutover_id,version_kind,
                          '2026-08-28T23:00:00Z','2026-08-28',
                          '2026-08-28T23:00:00Z',status,'sha256:causal-zero',
                          local_source_digest,source_watermarks_json,created_at
                     FROM sheet_vitrina_v1_warehouse_functional_versions
                    WHERE version_id='whfv_incident_source'"""
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                       cost_covered_quantity,quality,certified,wb_quantity,
                       wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                   SELECT 'whfv_causal_zero',warehouse_key,nm_id,quantity,wac_rub,
                          capital_rub,cost_covered_quantity,quality,certified,
                          wb_quantity,wb_in_way_to_client,wb_in_way_from_client,
                          provenance_json
                     FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id='whfv_incident_source'"""
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                       bundle_version,activated_at,as_of_date,snapshot_id,
                       plan_version,refreshed_at,plan_json)
                   SELECT 'causal-zero-bundle',activated_at,'2026-08-28',
                          'causal-zero-snapshot',plan_version,refreshed_at,
                          replace(plan_json,?,'2026-08-28')
                     FROM sheet_vitrina_v1_ready_snapshots
                    WHERE as_of_date=? LIMIT 1""",
                (DAY, DAY),
            )
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                (DAY,),
            )
            causal_zero = _discover_historical_manifests(
                conn,
                canonical_target={"accepted": True},
                storage_generation={"implicit": False, "query_only": True},
            )
            assert causal_zero["manifests"] == []
            assert causal_zero["selection"]["predicate"] == (
                "historical_b.exact_causal_handoff_debit_event"
            )
            assert causal_zero["selection"]["observed_cardinality"] == 0
            conn.execute("ROLLBACK TO causal_zero")
            conn.execute("RELEASE causal_zero")

            conn.execute("SAVEPOINT causal_many")
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_events(
                       event_id,cutover_id,order_id,episode_sequence,event_type,
                       source_order_observation_sequence,
                       source_status_observation_sequence,source_revision,
                       status_digest,supplier_status,wb_status,source_observed_at,
                       facility_id,pool,nm_id,quantity,physical_quantity_delta,
                       capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at)
                   SELECT 'handoff-debit-ambiguous',cutover_id,9909,episode_sequence,
                          event_type,source_order_observation_sequence,
                          source_status_observation_sequence,'ambiguous-revision',
                          ?,supplier_status,wb_status,source_observed_at,facility_id,
                          pool,nm_id,quantity,physical_quantity_delta,capital_delta_rub,
                          frozen_wac_rub,?,'2026-08-26T12:01:00Z'
                     FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events
                    WHERE event_id='handoff-debit-1'""",
                (
                    _fingerprint("ambiguous-status"),
                    _fingerprint("ambiguous-evidence"),
                ),
            )
            causal_many = _discover_historical_manifests(
                conn,
                canonical_target={"accepted": True},
                storage_generation={"implicit": False, "query_only": True},
            )
            assert causal_many["manifests"] == []
            assert causal_many["selection"]["observed_cardinality"] == 2
            conn.execute("ROLLBACK TO causal_many")
            conn.execute("RELEASE causal_many")
            event = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
                "WHERE event_id='handoff-debit-1'"
            ).fetchone()
            accepted_target = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id='whfv_incident_source' AND warehouse_key='ff' "
                "AND nm_id=?",
                (TARGET_NM_ID,),
            ).fetchone()
            current_pool_rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {BALANCES_TABLE} WHERE projection_epoch=1 "
                    "AND nm_id=? ORDER BY facility_id,pool,nm_id",
                    (TARGET_NM_ID,),
                ).fetchall()
            ]
            pool_before = list(
                conn.execute(
                    f"SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id"
                ).fetchall()
            )
            other_ready_before = conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version='other-date-bundle' AND as_of_date='2026-08-25'"
            ).fetchone()[0]
            conn.commit()
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime,
            timestamp_factory=lambda: "2026-08-28T09:00:00Z",
        )

        def forbid_hidden_dependency(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                "historical qualification must not open a hidden runtime dependency"
            )

        runtime.load_our_wb_cost_daily_state = forbid_hidden_dependency  # type: ignore[method-assign]
        runtime.store_registry.resolve = forbid_hidden_dependency  # type: ignore[method-assign]
        historical_manifest = {
            "schema": HISTORICAL_MANIFEST_SCHEMA,
            "business_date": DAY,
            "facility_id": FACILITY_ID,
            "pool": "FBS",
            "nm_ids": [TARGET_NM_ID],
            "accepted_version_id": "whfv_incident_source",
            "accepted_version_plan_digest": str(source["plan_fingerprint"]),
            "accepted_version_row_digest": _fingerprint(dict(source)),
            "accepted_target_row_digest": _fingerprint(dict(accepted_target)),
            "accepted_provenance_digest": _fingerprint(
                json.loads(str(accepted_target["provenance_json"]))
            ),
            "accepted_effective_at": str(source["effective_at"]),
            "accepted_published_at": str(source["published_at"]),
            "expected_current_active_version_id": current_version,
            "expected_current_sync_version_id": current_version,
            "expected_current_pool_digest": _fingerprint(current_pool_rows),
            "event_id": "handoff-debit-1",
            "event_source_digest": _fingerprint(str(event["source_revision"])),
            "event_status_digest": str(event["status_digest"]),
            "event_evidence_digest": str(event["evidence_digest"]),
            "event_row_digest": _fingerprint(
                {
                    key: event[key]
                    for key in (
                        "event_id",
                        "cutover_id",
                        "order_id",
                        "episode_sequence",
                        "event_type",
                        "source_order_observation_sequence",
                        "source_status_observation_sequence",
                        "source_revision",
                        "status_digest",
                        "supplier_status",
                        "wb_status",
                        "source_observed_at",
                        "facility_id",
                        "pool",
                        "nm_id",
                        "quantity",
                        "physical_quantity_delta",
                        "capital_delta_rub",
                        "frozen_wac_rub",
                        "evidence_digest",
                        "occurred_at",
                    )
                }
            ),
            "event_quantity_delta": str(event["physical_quantity_delta"]),
            "event_capital_delta_rub": str(event["capital_delta_rub"]),
            "event_wac_rub": str(event["frozen_wac_rub"]),
            "event_occurred_at": str(event["occurred_at"]),
            "accepted_quantity": "1952",
            "accepted_cost_covered_quantity": "1953",
            "accepted_capital_rub": "19520",
            "canonical_target": {"accepted": True},
            "storage_generation": {
                "implicit": False,
                "query_only": True,
                "manifest_sha256": "sha256:" + "a" * 64,
                "operational_generation_id": "fixture-generation",
            },
        }
        with sqlite3.connect(runtime.db_path) as forbidden_rw_dependency:
            forbidden_rw_dependency.row_factory = sqlite3.Row
            try:
                service.build_historical_plan(
                    historical_manifest,
                    dependency_conn=forbidden_rw_dependency,
                )
            except Exception as exc:
                assert (
                    getattr(exc, "code", "")
                    == "historical_dependency_session_not_query_only"
                )
            else:
                raise AssertionError(
                    "historical qualification must reject a read-write dependency session"
                )
        missing_binding = dict(historical_manifest)
        missing_binding.pop("event_row_digest")
        blocked = _build_historical_plan(service, runtime, missing_binding)
        assert blocked["status"] == UNSAFE_AMBIGUOUS
        assert blocked["reason"] == "historical_manifest_fields_invalid"
        plan = _build_historical_plan(service, runtime, historical_manifest)
        assert plan["status"] == REPAIRABLE, plan
        assert plan["mode"] == "historical"
        assert plan["nm_ids"] == [TARGET_NM_ID]
        assert WAC_CURRENT_ONLY_NM_ID not in plan["nm_ids"]
        assert plan["typed_evidence"]["before"] == {
            "quantity": "1952",
            "cost_covered_quantity": "1953",
        }
        assert plan["typed_evidence"]["after"]["quantity"] == "1952"
        assert plan["typed_evidence"]["after"]["cost_covered_quantity"] == "1952"
        assert len(LEGACY_WAC_161) == 161
        assert plan["typed_evidence"]["after"]["wac_rub"] == "10"
        historical_ff_line = next(
            item
            for item in plan["candidate"]["lines"]
            if item["warehouse_key"] == "ff" and item["nm_id"] == TARGET_NM_ID
        )
        historical_locations = json.loads(historical_ff_line["provenance_json"])[
            "source_records"
        ][-1]["locations"]
        assert [
            (item["facility_id"], item["quantity"], item["capital_rub"])
            for item in historical_locations
        ] == [
            (FACILITY_ID, "0", "0"),
            (MOSCOW_FACILITY_ID, "1952", "19520"),
        ]
        assert plan["typed_evidence"]["economics_dependency_closure"] == {
            "affected_positive_order_nm_ids": [TARGET_NM_ID],
            "missing_critical_total_dependencies_before": sorted(
                CRITICAL_TOTAL_METRIC_KEYS
            ),
            "missing_critical_total_dependencies_after": [],
            "critical_total_metric_keys": list(CRITICAL_TOTAL_METRIC_KEYS),
            "target_and_total_only": True,
            "ready_snapshot_count": 1,
        }
        assert all(update["non_target_digest"] for update in plan["ready_updates"])
        assert plan["bounds"]["current_pool_rows_used_as_candidate_source"] is False

        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                "SET wac_rub='11' WHERE version_id='whfv_incident_source' "
                "AND warehouse_key='ff' AND nm_id=?",
                (TARGET_NM_ID,),
            )
            conn.commit()
        changed = _build_historical_plan(service, runtime, historical_manifest)
        assert changed["reason"] == "accepted_historical_typed_digest_drift"
        for invalid_wac in (None, "-10"):
            with sqlite3.connect(runtime.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                    "SET wac_rub=? WHERE version_id='whfv_incident_source' "
                    "AND warehouse_key='ff' AND nm_id=?",
                    (invalid_wac, TARGET_NM_ID),
                )
                invalid_target = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
                    "WHERE version_id='whfv_incident_source' AND warehouse_key='ff' "
                    "AND nm_id=?",
                    (TARGET_NM_ID,),
                ).fetchone()
                conn.commit()
            invalid_manifest = dict(historical_manifest)
            invalid_manifest["accepted_target_row_digest"] = _fingerprint(
                dict(invalid_target)
            )
            invalid = _build_historical_plan(service, runtime, invalid_manifest)
            assert invalid["reason"] == "historical_accepted_wac_invalid"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                "SET wac_rub=? WHERE version_id='whfv_incident_source' "
                "AND warehouse_key='ff' AND nm_id=?",
                (LEGACY_WAC_161, TARGET_NM_ID),
            )
            conn.commit()
        applied = service.apply_plan(
            plan,
            confirm_fingerprint=str(plan["plan_fingerprint"]),
            approval_reference="WBC-0013-SSS006-fixture",
            actor="dense-smoke",
        )
        assert applied["status"] == REPAIRED and not applied["idempotent"], applied
        repeated = service.apply_plan(
            plan,
            confirm_fingerprint=str(plan["plan_fingerprint"]),
            approval_reference="WBC-0013-SSS006-fixture",
            actor="dense-smoke",
        )
        assert repeated["status"] == REPAIRED and repeated["idempotent"]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            assert (
                conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active "
                    "WHERE slot=1"
                ).fetchone()[0]
                == current_version
            )
            assert (
                conn.execute(
                    "SELECT active_version_id FROM sheet_vitrina_v1_warehouse_wb_sync_status "
                    "WHERE slot=1"
                ).fetchone()[0]
                == current_version
            )
            repaired = conn.execute(
                """SELECT quantity,cost_covered_quantity,wac_rub
                     FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                (str(plan["target_version_id"]), TARGET_NM_ID),
            ).fetchone()
            assert tuple(repaired) == ("1952", "1952", "10")
            assert (
                list(
                    conn.execute(
                        f"SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id"
                    ).fetchall()
                )
                == pool_before
            )
            assert (
                conn.execute(
                    "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                    "WHERE bundle_version='other-date-bundle' AND as_of_date='2026-08-25'"
                ).fetchone()[0]
                == other_ready_before
            )
            cells = _ready_cells(conn)
            assert cells[
                f"SKU:{TARGET_NM_ID}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}"
            ] not in {
                None,
                "",
            }
            assert all(
                cells[f"TOTAL|{key}"] not in {None, ""}
                for key in CRITICAL_TOTAL_METRIC_KEYS
            )


def _test_incident_plan_apply_idempotency_and_bounds() -> None:
    with tempfile.TemporaryDirectory(prefix="fbs-material-repair-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True, noise_rows=1_000)
        clock = _Clock()
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=clock
        )
        started = time.perf_counter()
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        assert plan["status"] == REPAIRABLE, plan
        evidence = plan["typed_evidence"]
        assert evidence["affected_positive_order_sku_count"] == 1
        assert evidence["invariant_mismatch"]["reason_codes"] == [
            "ff_cost_coverage_incomplete",
            "ff_stage_evidence_mismatch",
            "missing_facility_pool_evidence",
        ]
        assert set(evidence["missing_critical_total_dependencies_before"]) == set(
            CRITICAL_TOTAL_METRIC_KEYS
        )
        assert evidence["missing_critical_total_dependencies_after"] == []
        assert {
            key: plan["bounds"][key]
            for key in (
                "target_count",
                "ready_snapshot_count",
                "functional_balance_rows",
                "full_database_copy",
                "external_source_calls",
                "full_day_reload",
            )
        } == {
            "target_count": 1,
            "ready_snapshot_count": 1,
            "functional_balance_rows": 12,
            "full_database_copy": False,
            "external_source_calls": 0,
            "full_day_reload": False,
        }
        assert 0 < plan["bounds"]["ready_before_bytes"] <= MAX_READY_CLOSURE_BYTES
        assert 0 < plan["bounds"]["ready_after_bytes"] <= MAX_READY_CLOSURE_BYTES
        assert plan["bounds"]["max_persisted_plan_bytes"] == MAX_PERSISTED_PLAN_BYTES
        plan_bytes = len(json.dumps(plan, default=str).encode("utf-8"))
        assert plan_bytes < 1_000_000
        assert elapsed_ms < 2_000, elapsed_ms
        before = _fingerprints(runtime.db_path)
        applied = service.apply_plan(plan, confirm_fingerprint=plan["plan_fingerprint"])
        assert applied["status"] == REPAIRED and not applied["idempotent"]
        repeated = service.apply_plan(
            plan, confirm_fingerprint=plan["plan_fingerprint"]
        )
        assert repeated["status"] == REPAIRED and repeated["idempotent"]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            assert _active_ff(conn, TARGET_NM_ID)[0:2] == ("1952", "1952")
            cells = _ready_cells(conn)
            assert all(
                cells[f"TOTAL|{key}"] not in {None, ""}
                for key in CRITICAL_TOTAL_METRIC_KEYS
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_fbs_material_intents"
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_fbs_material_intent_events"
                ).fetchone()[0]
                >= 3
            )
            storage_bytes = {
                "intent_bytes": int(
                    conn.execute(
                        """SELECT COALESCE(SUM(
                               length(operation_id)+length(plan_fingerprint)+
                               length(plan_json)+
                               length(typed_evidence_json)+length(COALESCE(last_error,''))
                           ),0) FROM sheet_vitrina_v1_warehouse_fbs_material_intents"""
                    ).fetchone()[0]
                ),
                "event_bytes": int(
                    conn.execute(
                        """SELECT COALESCE(SUM(
                               length(operation_id)+length(status)+length(evidence_json)
                           ),0) FROM sheet_vitrina_v1_warehouse_fbs_material_intent_events"""
                    ).fetchone()[0]
                ),
                "candidate_balance_bytes": int(
                    conn.execute(
                        """SELECT COALESCE(SUM(
                               length(warehouse_key)+length(quantity)+
                               length(capital_rub)+length(COALESCE(wac_rub,''))+
                               length(provenance_json)
                           ),0)
                           FROM sheet_vitrina_v1_warehouse_functional_balances
                           WHERE version_id=?""",
                        (plan["target_version_id"],),
                    ).fetchone()[0]
                ),
                "ready_snapshot_bytes": int(
                    conn.execute(
                        """SELECT length(plan_json)
                           FROM sheet_vitrina_v1_ready_snapshots
                           WHERE bundle_version='incident-bundle' AND as_of_date=?""",
                        (DAY,),
                    ).fetchone()[0]
                ),
            }
        after = _fingerprints(runtime.db_path)
        assert before["non_target"] == after["non_target"]
        assert before["reservations"] == after["reservations"]
        assert before["orders"] == after["orders"]
        assert before["source_history"] == after["source_history"]
        assert before["ready_non_target_sentinel"] == after["ready_non_target_sentinel"]
        print(
            json.dumps(
                {
                    "benchmark": "single_sku_incident_with_unrelated_noise",
                    "elapsed_ms": elapsed_ms,
                    "plan_bytes": plan_bytes,
                    "functional_balance_rows": plan["bounds"][
                        "functional_balance_rows"
                    ],
                    "unrelated_noise_rows": 1_000,
                    "full_database_copy": False,
                    **storage_bytes,
                },
                sort_keys=True,
            )
        )


def _test_resume_before_and_after_commit_transport_loss() -> None:
    with tempfile.TemporaryDirectory(prefix="fbs-material-before-commit-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )

        def before_commit(phase: str) -> None:
            if phase == "before_commit":
                raise ConnectionError("simulated transport loss before commit")

        interrupted = service.apply_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            transport_hook=before_commit,
        )
        assert interrupted["status"] == REPAIRABLE
        # A new service instance has no process-local plan state; it resumes
        # from the bounded plan persisted with the durable intent.
        resumed = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        ).resume(operation_id=plan["operation_id"])
        assert resumed["status"] == REPAIRED and not resumed["idempotent"]

    with tempfile.TemporaryDirectory(prefix="fbs-material-after-commit-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )

        def after_commit(phase: str) -> None:
            if phase == "after_commit":
                raise ConnectionError("simulated ambiguous response after commit")

        reconciled = service.apply_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            transport_hook=after_commit,
        )
        assert reconciled["status"] == REPAIRED and reconciled["idempotent"]
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_functional_versions "
                    "WHERE version_id=?",
                    (plan["target_version_id"],),
                ).fetchone()[0]
                == 1
            )

    with tempfile.TemporaryDirectory(prefix="fbs-material-retry-budget-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )

        def always_before_commit(phase: str) -> None:
            if phase == "before_commit":
                raise ConnectionError("repeatable loss before commit")

        outcomes = [
            service.apply_plan(
                plan,
                confirm_fingerprint=plan["plan_fingerprint"],
                transport_hook=always_before_commit,
            )
            for _ in range(3)
        ]
        assert [item["status"] for item in outcomes] == [
            REPAIRABLE,
            REPAIRABLE,
            RETRY_EXHAUSTED,
        ]
        assert outcomes[-1]["attempt_count"] == 3
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
                ).fetchone()[0]
                == "whfv_incident_source"
            )

    with tempfile.TemporaryDirectory(prefix="fbs-material-unknown-loss-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )

        def unknown_before_commit(phase: str) -> None:
            if phase == "before_commit":
                raise RuntimeError("unknown transport outcome")

        unsafe = service.apply_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            transport_hook=unknown_before_commit,
        )
        assert unsafe["status"] == UNSAFE_AMBIGUOUS
        assert unsafe["attempt_count"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
                ).fetchone()[0]
                == "whfv_incident_source"
            )

    for label, error in (
        ("timeout", TimeoutError("bounded transport timeout")),
        ("sqlite-lock", sqlite3.OperationalError("database is locked")),
    ):
        outcome = _precommit_failure_outcome(error=error, label=label)
        assert outcome["status"] == REPAIRABLE, (label, outcome)
        assert outcome["attempt_count"] == 1, (label, outcome)

    for label, error in (
        ("permission", PermissionError(errno.EACCES, "permission denied")),
        ("missing-path", FileNotFoundError(errno.ENOENT, "path not found")),
        ("capacity", OSError(errno.ENOSPC, "no space left on device")),
    ):
        outcome = _precommit_failure_outcome(error=error, label=label)
        assert outcome["status"] == UNSAFE_AMBIGUOUS, (label, outcome)
        assert outcome["attempt_count"] == 1, (label, outcome)


def _precommit_failure_outcome(*, error: Exception, label: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"fbs-material-{label}-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )

        def fail_before_commit(phase: str) -> None:
            if phase == "before_commit":
                raise error

        outcome = service.apply_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            transport_hook=fail_before_commit,
        )
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
                ).fetchone()[0]
                == "whfv_incident_source"
            )
        return outcome


def _test_drift_concurrency_and_fail_closed_boundaries() -> None:
    with tempfile.TemporaryDirectory(prefix="fbs-material-drift-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {BALANCES_TABLE} SET source_watermark='drift' "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=?",
                (FACILITY_ID, TARGET_NM_ID),
            )
            conn.commit()
        try:
            service.apply_plan(plan, confirm_fingerprint=plan["plan_fingerprint"])
        except Exception as exc:
            assert getattr(exc, "code", "") == "repair_source_material_cas_drift"
        else:
            raise AssertionError(
                "under-lock CAS drift must reject before durable intent"
            )
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_fbs_material_intents"
                ).fetchone()[0]
                == 0
            )

    with tempfile.TemporaryDirectory(prefix="fbs-material-source-proof-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""UPDATE {BALANCES_TABLE} SET source_watermark='unknown-effect'
                    WHERE facility_id=? AND pool='FBS' AND nm_id=?""",
                (FACILITY_ID, TARGET_NM_ID),
            )
            conn.commit()
        blocked = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        ).build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )
        assert blocked["status"] == UNSAFE_AMBIGUOUS
        assert blocked["reason"] == "target_source_evidence_missing_or_ambiguous"

    with tempfile.TemporaryDirectory(prefix="fbs-material-no-local-drift-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=False)
        blocked = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        ).build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )
        assert blocked["status"] == UNSAFE_AMBIGUOUS
        assert blocked["reason"] == "target_facility_pool_not_mismatched"

    with tempfile.TemporaryDirectory(prefix="fbs-material-snapshot-gap-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=False)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id='whfv_incident_source'"
            )
            conn.execute(
                f"""UPDATE {BALANCES_TABLE}
                    SET quantity=1952,capital_rub='19520',wac_rub='10',
                        source_watermark='handoff-debit-1'
                    WHERE facility_id=? AND pool='FBS' AND nm_id=?""",
                (FACILITY_ID, TARGET_NM_ID),
            )
            try:
                publish_fbs_pool_aggregate_revision(
                    conn,
                    affected_nm_ids=[TARGET_NM_ID],
                    source_kind="fbs_order_lifecycle_event",
                    source_id="handoff-debit-1",
                    business_date=DAY,
                    published_at="2026-08-26T12:01:00Z",
                )
            except Exception as exc:
                assert getattr(exc, "code", "") == "fbs_material_snapshot_missing"
                conn.rollback()
            else:
                raise AssertionError("missing canonical snapshot must fail closed")
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
                ).fetchone()[0]
                == "whfv_incident_source"
            )

    with tempfile.TemporaryDirectory(prefix="fbs-material-broad-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        with sqlite3.connect(runtime.db_path) as conn:
            active = conn.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()[0]
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET cost_covered_quantity='20'
                   WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                (active, NON_TARGET_NM_ID),
            )
            conn.commit()
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        broad = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )
        assert broad["status"] == UNSAFE_AMBIGUOUS
        assert broad["reason"] == "broad_or_unknown_mismatch"
        assert (
            service.build_plan(
                business_date=DAY,
                facility_id=FACILITY_ID,
                pool="FBO",
                nm_ids=[TARGET_NM_ID],
            )["status"]
            == UNSAFE_AMBIGUOUS
        )
        assert (
            service.build_plan(
                business_date="2026-08-25",
                facility_id=FACILITY_ID,
                pool="FBS",
                nm_ids=[TARGET_NM_ID],
            )["status"]
            == HISTORICAL_RECOVERY_REQUIRED
        )

    with tempfile.TemporaryDirectory(prefix="fbs-material-lock-") as temp_dir:
        runtime = _seed(Path(temp_dir), mixed=True)
        service = WarehouseFbsMaterialRematerializer(
            runtime=runtime, timestamp_factory=_Clock()
        )
        plan = service.build_plan(
            business_date=DAY,
            facility_id=FACILITY_ID,
            pool="FBS",
            nm_ids=[TARGET_NM_ID],
        )
        result: list[dict[str, object]] = []
        with warehouse_functional_write_lock(runtime.runtime_dir):
            worker = threading.Thread(
                target=lambda: result.append(
                    service.apply_plan(
                        plan, confirm_fingerprint=plan["plan_fingerprint"]
                    )
                )
            )
            worker.start()
            time.sleep(0.1)
            assert worker.is_alive(), (
                "material repair must wait for the shared writer lock"
            )
        worker.join(timeout=10)
        assert not worker.is_alive() and result[0]["status"] == REPAIRED


def _seed(
    root: Path,
    *,
    mixed: bool,
    noise_rows: int = 0,
) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root / "runtime")
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime.load_current_state()
    except ValueError as exc:
        assert str(exc) == "runtime current state is not materialized"
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_ff_pool_foundation_schema(conn)
        ensure_warehouse_fbs_material_schema(conn)
        ensure_proxy_v4_schema(conn)
        CalculationParametersBlock(runtime=runtime).ensure_initial_version(
            connection=conn, created_at=NOW
        )
        conn.execute(
            f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,?,?,?)",
            (FACILITY_ID, "INC", "Incident FF", 1, "Asia/Yekaterinburg", NOW, NOW),
        )
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,1,1,?,?,?)",
            ("fbs-material-smoke", NOW, "{}"),
        )
        for nm_id, quantity, capital, wac in (
            (
                TARGET_NM_ID,
                1952 if mixed else 1953,
                "19520" if mixed else "19530",
                "10",
            ),
            (NON_TARGET_NM_ID, 21, "420", "20"),
        ):
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at)
                   VALUES(?, 'FBS', ?,1,?,?,?,?,?)""",
                (
                    FACILITY_ID,
                    nm_id,
                    quantity,
                    capital,
                    wac,
                    "event-handoff-101" if nm_id == TARGET_NM_ID else f"pool-{nm_id}",
                    NOW,
                ),
            )
        for event_id, order_id, occurred_at in (
            ("event-handoff-101", 9001, NOW),
            ("handoff-debit-1", 9002, "2026-08-26T12:01:00Z"),
        ):
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_events(
                       event_id,cutover_id,order_id,episode_sequence,event_type,
                       source_order_observation_sequence,
                       source_status_observation_sequence,source_revision,
                       status_digest,supplier_status,wb_status,source_observed_at,
                       facility_id,pool,nm_id,quantity,physical_quantity_delta,
                       capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at)
                   VALUES(?,?,?,1,'handoff_debit',1,1,?,?,
                          'complete','sorted',?,?,'FBS',?,1,-1,'-10','10',?,?)""",
                (
                    event_id,
                    "warehouse_functional_cutover_v1",
                    order_id,
                    f"revision-{order_id}",
                    _fingerprint(f"status-{order_id}"),
                    occurred_at,
                    FACILITY_ID,
                    TARGET_NM_ID,
                    _fingerprint(f"evidence-{order_id}"),
                    occurred_at,
                ),
            )
        for offset in range(noise_rows):
            noise_nm_id = 10_000 + offset
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at)
                   VALUES(?, 'FBO', ?,1,0,'0',NULL,?,?)""",
                (FACILITY_ID, noise_nm_id, f"noise-{noise_nm_id}", NOW),
            )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                   cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,
                   absorbed_supply_revisions_json,backup_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "warehouse_functional_cutover_v1",
                NOW,
                "posted",
                "sha256:cutover-smoke",
                "{}",
                "{}",
                "{}",
                NOW,
                NOW,
            ),
        )
        source_version = "whfv_incident_source"
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                   version_id,cutover_id,version_kind,effective_at,
                   business_effective_date,published_at,status,plan_fingerprint,
                   local_source_digest,source_watermarks_json,created_at)
               VALUES(?,?,?,?,?,?,'good',?,?,?,?)""",
            (
                source_version,
                "warehouse_functional_cutover_v1",
                "hourly_wb_sync",
                NOW,
                DAY,
                NOW,
                _fingerprint("source-version"),
                _fingerprint("source-local"),
                "{}",
                NOW,
            ),
        )
        for nm_id in (TARGET_NM_ID, NON_TARGET_NM_ID):
            for stage in STAGES:
                if nm_id == TARGET_NM_ID and stage == "ff":
                    quantity = "1952" if mixed else "1953"
                    capital = "19520" if mixed else "19530"
                    wac = LEGACY_WAC_161 if mixed else "10"
                    covered = "1953"
                    locations_quantity = "1953"
                    locations_capital = "19530"
                elif nm_id == NON_TARGET_NM_ID and stage == "ff":
                    quantity, capital, wac, covered = "21", "420", "20", "21"
                    locations_quantity, locations_capital = "21", "420"
                elif nm_id == TARGET_NM_ID and stage == "wb":
                    quantity, capital, wac, covered = "10", "200", "20", "10"
                    locations_quantity, locations_capital = "0", "0"
                else:
                    quantity, capital, wac, covered = "0", "0", None, "0"
                    locations_quantity, locations_capital = "0", "0"
                provenance = {
                    "source_records": (
                        [
                            {
                                "source": "pool",
                                "flow_quantity": quantity,
                                "flow_capital_rub": capital,
                                "cost_freshness": "exact",
                                "quality": "exact",
                                "expenses_complete_certification": True,
                                "locations": (
                                    [
                                        {
                                            "facility_id": FACILITY_ID,
                                            "pool": "FBS",
                                            "quantity": "1",
                                            "capital_rub": "10",
                                        },
                                        {
                                            "facility_id": MOSCOW_FACILITY_ID,
                                            "pool": "FBS",
                                            "quantity": "1952",
                                            "capital_rub": "19520",
                                        },
                                    ]
                                    if mixed and nm_id == TARGET_NM_ID and stage == "ff"
                                    else [
                                        {
                                            "facility_id": FACILITY_ID,
                                            "pool": "FBS",
                                            "quantity": locations_quantity,
                                            "capital_rub": locations_capital,
                                        }
                                    ]
                                ),
                            }
                        ]
                        if stage == "ff"
                        else []
                    )
                }
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                           version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                           cost_covered_quantity,quality,certified,wb_quantity,
                           wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source_version,
                        stage,
                        nm_id,
                        quantity,
                        wac,
                        capital,
                        covered,
                        "exact",
                        1,
                        quantity if stage == "wb" else "0",
                        "0",
                        "0",
                        json.dumps(provenance, sort_keys=True),
                    ),
                )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                   snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
                   pagination_complete,page_count,page_offsets_json,raw_row_count,
                   raw_rows_digest,raw_rows_json,items_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "snapshot-source",
                source_version,
                NOW,
                DAY,
                json.dumps([TARGET_NM_ID, NON_TARGET_NM_ID]),
                1,
                1,
                "[0]",
                2,
                "sha256:raw",
                "[]",
                json.dumps(
                    [
                        {"nm_id": TARGET_NM_ID, "quantity": 10},
                        {"nm_id": NON_TARGET_NM_ID, "quantity": 0},
                    ]
                ),
                NOW,
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,?,?)",
            (source_version, NOW),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_ff_reservations(
                   version_id,supply_id,nm_id,quantity) VALUES(?,?,?,?)""",
            (source_version, "supply-non-target", NON_TARGET_NM_ID, "2"),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                   slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at)
               VALUES(1,?,?,NULL,?,?)""",
            (NOW, NOW, source_version, NOW),
        )
        conn.execute(
            """INSERT OR IGNORE INTO registry_upload_versions(
                   bundle_version,uploaded_at,activated_at) VALUES(?,?,?)""",
            ("incident-bundle", NOW, NOW),
        )
        v4 = _proxy_v4_parameters()
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions(
                   version_id,block_key,revision,effective_date,source_window_from,
                   source_window_to,source_window_fingerprint,parameters_json,
                   fingerprint,version_kind,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "proxy-v4-incident",
                "proxy_profit_margin_v4",
                1,
                v4.effective_date,
                v4.source_window_from,
                v4.source_window_to,
                v4.source_window_fingerprint,
                json.dumps(v4.public(), sort_keys=True),
                "sha256:proxy-v4-incident",
                "historical_initialization",
                "smoke",
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
                   observation_id,order_id,source_revision,supply_id,delivery_type,
                   source_created_at,warehouse_id,office_id,nm_id,chrt_id,
                   seller_sku,rid_sha256,order_uid_sha256,skus_json,cargo_type,
                   cross_border_type,is_zero_order,observed_at,collector_date_from,
                   collector_date_to,collector_cursor)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "observation-non-target",
                9100,
                "revision-non-target",
                "supply-non-target",
                "fbs",
                NOW,
                854205,
                12223,
                NON_TARGET_NM_ID,
                2020,
                "NON-TARGET",
                "sha256:rid-non-target",
                "sha256:uid-non-target",
                "[]",
                0,
                0,
                0,
                NOW,
                20260826,
                20260826,
                0,
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_current(
                   order_id,order_revision,status_digest,supplier_status,wb_status,
                   source_observed_at,local_first_seen_at,local_last_seen_at,
                   observation_count,episode_sequence)
               VALUES(?,?,?,?,?,?,?,?,1,1)""",
            (
                9100,
                "revision-non-target",
                "status-non-target",
                "confirm",
                "waiting",
                NOW,
                NOW,
                NOW,
            ),
        )
        conn.commit()
    _seed_ready_snapshot(runtime, mixed=mixed)
    return runtime


def _seed_ready_snapshot(
    runtime: RegistryUploadDbBackedRuntime, *, mixed: bool
) -> None:
    base = {
        "date_columns": [DAY],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "write_start_cell": "A1",
                "header": ["Показатель", "row_id", DAY],
                "rows": [
                    ["Target orders", f"SKU:{TARGET_NM_ID}|orderSum", 100000],
                    ["Target count", f"SKU:{TARGET_NM_ID}|orderCount", 10],
                    ["Target ads", f"SKU:{TARGET_NM_ID}|ads_sum", 1000],
                    ["Other orders", f"SKU:{NON_TARGET_NM_ID}|orderSum", 0],
                    ["Other count", f"SKU:{NON_TARGET_NM_ID}|orderCount", 0],
                    ["Other ads", f"SKU:{NON_TARGET_NM_ID}|ads_sum", 0],
                    ["Non target sentinel", f"SKU:{NON_TARGET_NM_ID}|sentinel", 777],
                ],
            }
        ],
    }
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        source_version = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()[0]
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=?",
                (source_version,),
            ).fetchall()
        ]
    warehouse = _warehouse_metric_lookup(
        rows,
        version_id=str(source_version),
        published_at=NOW,
        source_watermarks={},
        requested_nm_ids=[TARGET_NM_ID, NON_TARGET_NM_ID],
    )
    costs = build_inventory_cost_blend_lookup(
        as_of_date=DAY,
        wb_compat_lookup={},
        product_capital_lookup=warehouse,
    )
    params = CalculationParametersBlock(runtime=runtime).parameters_for_date(DAY)
    transformed = _transform_snapshot(
        {
            "bundle_version": "incident-bundle",
            "as_of_date": DAY,
            "plan_json": json.dumps(base),
        },
        costs={DAY: costs},
        warehouse_metrics={DAY: warehouse},
        warehouse_exact_dates={DAY},
        warehouse_covered_nm_ids={DAY: {TARGET_NM_ID, NON_TARGET_NM_ID}},
        warehouse_version_ids={DAY: str(source_version)},
        parameters={DAY: params},
        proxy_v4_parameters={DAY: _proxy_v4_parameters()},
        source_fingerprint="sha256:baseline",
        cutover_business_date=DAY,
        operation_business_date=DAY,
    )
    payload = json.loads(transformed["after_plan_json"])
    if mixed:
        cells = _payload_rows(payload)
        for key in CRITICAL_TOTAL_METRIC_KEYS:
            cells[f"TOTAL|{key}"][2] = None
        for key in (
            OUR_WB_UNIT_COST_RUB_METRIC_KEY,
            OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
            OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
            PROXY_V4_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_MARGIN_PCT_METRIC_KEY,
            PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        ):
            cells[f"SKU:{TARGET_NM_ID}|{key}"][2] = None
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,
                   plan_version,refreshed_at,plan_json)
               VALUES(?,?,?,?,?,?,?)""",
            (
                "incident-bundle",
                NOW,
                DAY,
                "incident-ready",
                "v1",
                NOW,
                json.dumps(payload, sort_keys=True),
            ),
        )
        conn.commit()


def _proxy_v4_parameters() -> ProxyV4Parameters:
    return ProxyV4Parameters(
        effective_date="2026-08-22",
        buyout_rate=Decimal("1"),
        tax_rate=Decimal("0"),
        agent_remuneration_rate=Decimal("0"),
        acquiring_rate=Decimal("0"),
        wb_logistics_rate=Decimal("0"),
        wb_storage_rate=Decimal("0"),
        penalties_adjustments_rate=Decimal("0"),
        other_expense_rate=Decimal("0"),
        source_window_from="2026-08-10",
        source_window_to="2026-08-16",
        source_window_fingerprint="sha256:synthetic",
        source_week_ranges=(("2026-08-10", "2026-08-16"),),
        source_slot_from="2026-08-10",
        source_slot_to="2026-08-16",
        buyout_order_count_weight=Decimal("1"),
        finance_net_revenue_weight=Decimal("1"),
        formula_version=PROXY_V4_FORMULA_VERSION,
        version_id="proxy-v4-incident",
        revision=1,
        version_kind="historical_initialization",
    )


def _active_ff(conn: sqlite3.Connection, nm_id: int) -> tuple[str, str, str, str]:
    row = conn.execute(
        """SELECT balance.quantity,balance.cost_covered_quantity,
                  balance.capital_rub,balance.wac_rub
           FROM sheet_vitrina_v1_warehouse_functional_active active
           JOIN sheet_vitrina_v1_warehouse_functional_balances balance
             ON balance.version_id=active.version_id
           WHERE active.slot=1 AND balance.warehouse_key='ff' AND balance.nm_id=?""",
        (nm_id,),
    ).fetchone()
    return tuple(row)  # type: ignore[return-value]


def _ready_cells(conn: sqlite3.Connection) -> dict[str, object]:
    payload = json.loads(
        conn.execute(
            "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version='incident-bundle'"
        ).fetchone()[0]
    )
    return {key: row[2] for key, row in _payload_rows(payload).items()}


def _payload_rows(payload: dict[str, object]) -> dict[str, list[object]]:
    sheet = next(
        item
        for item in payload["sheets"]  # type: ignore[index,union-attr]
        if item["sheet_name"] == "DATA_VITRINA"  # type: ignore[index]
    )
    return {str(row[1]): row for row in sheet["rows"]}  # type: ignore[index]


def _fingerprints(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()[0]
        non_target = conn.execute(
            """SELECT warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                      cost_covered_quantity,quality,certified,wb_quantity,
                      wb_in_way_to_client,wb_in_way_from_client
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND nm_id=? ORDER BY warehouse_key""",
            (active, NON_TARGET_NM_ID),
        ).fetchall()
        source_history = conn.execute(
            """SELECT version_id,warehouse_key,nm_id,quantity,capital_rub,
                      cost_covered_quantity
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id='whfv_incident_source' ORDER BY warehouse_key,nm_id"""
        ).fetchall()
        reservations = conn.execute(
            """SELECT supply_id,nm_id,quantity
               FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
               WHERE version_id=? ORDER BY supply_id,nm_id""",
            (active,),
        ).fetchall()
        orders = conn.execute(
            """SELECT observation.order_id,observation.source_revision,
                      observation.supply_id,observation.nm_id,
                      current.supplier_status,current.wb_status,
                      current.observation_count,current.episode_sequence
               FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations observation
               JOIN sheet_vitrina_v1_wb_supplies_fbs_status_current current
                 ON current.order_id=observation.order_id
               WHERE observation.nm_id=? ORDER BY observation.order_id""",
            (NON_TARGET_NM_ID,),
        ).fetchall()
        ready_non_target = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='incident-bundle' AND as_of_date=?""",
            (DAY,),
        ).fetchone()
        ready_sentinel = (
            _ready_cells(conn).get(f"SKU:{NON_TARGET_NM_ID}|sentinel")
            if ready_non_target is not None
            else None
        )
    return {
        "non_target": [tuple(row) for row in non_target],
        "source_history": [tuple(row) for row in source_history],
        "reservations": [tuple(row) for row in reservations],
        "orders": [tuple(row) for row in orders],
        "ready_non_target_sentinel": ready_sentinel,
    }


if __name__ == "__main__":
    main()
