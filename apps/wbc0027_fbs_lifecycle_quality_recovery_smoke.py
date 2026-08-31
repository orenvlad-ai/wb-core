#!/usr/bin/env python3
"""Deterministic bounded recovery, history supersession and no-op smoke."""

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

from apps.ff_pool_fbs_forward_recovery_smoke import (  # noqa: E402
    SHA,
    _RecoveryClock,
    _insert_backlog,
    _prepared_runtime,
)
from apps.ff_pool_fbs_lifecycle_smoke import (  # noqa: E402
    _append_status,
    _insert_post_t_order,
)
from apps import wbc0027_fbs_lifecycle_quality_recovery as module  # noqa: E402
from apps import wbc0027_fbs_mapping_extension as mapping_module  # noqa: E402
from packages.application.ff_pool_fbs_forward_recovery import (  # noqa: E402
    FfPoolFbsForwardRecoveryMutation,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    fbs_lifecycle_quality_coverage,
    process_post_t_fbs_lifecycle,
)
from packages.application.own_product_capital import (  # noqa: E402
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    _apply_fbs_lifecycle_quality_to_product_lookup,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    ensure_inventory_history_schema,
    read_inventory_history_window,
)


class _Clock:
    def __init__(self) -> None:
        self.second = 0

    def __call__(self) -> str:
        self.second += 1
        return f"2026-08-31T12:00:{self.second:02d}Z"


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _prepared_runtime(root / "runtime")
        _insert_backlog(runtime.db_path)
        forward = FfPoolFbsForwardRecoveryMutation(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            timestamp_factory=_RecoveryClock(),
        )
        forward_plan = forward.build_plan()
        forward.apply(
            forward_plan,
            fingerprint=str(forward_plan["fingerprint"]),
            approval_reference="synthetic-forward-gate",
            actor="smoke",
            evidence_dir=root / "forward-evidence",
        )

        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_ff_pool_fbs_lifecycle_schema(conn)
            ensure_inventory_history_schema(conn)
            _seed_second_facility_and_balances(conn)
            _insert_post_t_order(
                conn,
                order_id=9701,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T01:00:00Z",
                observed_at="2026-08-17T01:01:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=998,
                source_chrt_id=1998,
                seller_sku="seller-998",
                barcode="sku-998",
            )
            _insert_post_t_order(
                conn,
                order_id=9702,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T02:00:00Z",
                observed_at="2026-08-17T02:01:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=997,
                source_chrt_id=1997,
                seller_sku="seller-997",
                barcode="sku-997",
            )
            _insert_custom_order(
                conn,
                order_id=9703,
                warehouse_id=502,
                source_nm_id=996,
                source_chrt_id=1996,
                seller_sku="seller-996",
                barcode="sku-996",
                source_created_at="2026-08-17T03:00:00Z",
                observed_at="2026-08-17T03:01:00Z",
            )
            _insert_post_t_order(
                conn,
                order_id=9704,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T03:10:00Z",
                observed_at="2026-08-17T03:11:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=996,
                source_chrt_id=1996,
                seller_sku="seller-996",
                barcode="sku-996",
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            processed = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-17T04:00:00Z",
                limit=100,
                schema_ready=True,
            )
            conn.commit()
            assert processed["summary"]["identity_pending"] == 4
            _add_later_canonical_identity(conn)
            _seed_mapping_owner(conn)
            _seed_history(conn)
            cutoff = int(
                conn.execute(
                    "SELECT MAX(source_status_observation_sequence) "
                    f"FROM {IDENTITY_PENDING_TABLE}"
                ).fetchone()[0]
            )
            assert conn.execute(
                f"""SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE resolution.pending_id IS NULL"""
            ).fetchone()[0] == 5
            coverage = fbs_lifecycle_quality_coverage(
                conn,
                as_of_date="2026-08-17",
                requested_nm_ids={101, 102, 103},
            )
            assert coverage["status"] == "partial"
            assert {
                (str(item["facility_id"]), int(item["nm_id"]))
                for item in coverage["groups"]
                if item.get("nm_id") is not None
            }.issuperset({("fac_moscow", 101), ("fac_moscow", 102)})
            capital_lookup = {
                102: {
                    "_inventory_cost_stages": {
                        "FF": {"quantity": 100, "wac_rub": "10", "certified": True}
                    }
                }
            }
            _apply_fbs_lifecycle_quality_to_product_lookup(
                capital_lookup,
                coverage=coverage,
            )
            assert capital_lookup[102]["presentation_state"] == "unavailable"
            assert capital_lookup[102][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] is None
            assert capital_lookup[102]["_inventory_cost_stages"]["FF"]["wac_rub"] is None
            conn.commit()

        historical = read_inventory_history_window(
            runtime.db_path,
            dates=["2026-08-17"],
            current_date="2026-08-31",
        )
        historical_scope = historical["dates"]["2026-08-17"]["scopes"]["SKU:102"]
        assert historical_scope["quality"] == "partial"
        assert historical_scope["facilities"]["fac_moscow"]["state"] == "missing"

        original = (
            module.SOURCE_CUTOFF_SEQUENCE,
            module.MOSCOW_FACILITY_ID,
            module.ORENBURG_FACILITY_ID,
            module.TARGET_GROUPS,
            module.TARGET_GROUP_SET,
            module.EXACT_MAPPING_TUPLE,
        )
        module.SOURCE_CUTOFF_SEQUENCE = cutoff
        module.MOSCOW_FACILITY_ID = "fac_moscow"
        module.ORENBURG_FACILITY_ID = "fac_orenburg"
        module.TARGET_GROUPS = (
            ("fac_moscow", 101),
            ("fac_moscow", 102),
            ("fac_moscow", 103),
            ("fac_orenburg", 103),
        )
        module.TARGET_GROUP_SET = frozenset(module.TARGET_GROUPS)
        module.EXACT_MAPPING_TUPLE = {
            "source_nm_id": 996,
            "source_chrt_id": 1996,
            "source_barcode": "sku-996",
            "source_sku": "seller-996",
            "target_nm_id": 103,
        }
        try:
            runner = module.Wbc0027FbsLifecycleQualityRecovery(
                runtime_dir=runtime.runtime_dir,
                deployed_sha=SHA,
                timestamp_factory=_Clock(),
            )
            blocked_plan = runner.build_plan()
            assert blocked_plan["apply_allowed"] is False
            assert blocked_plan["blockers"] == [
                "exact_four_group_coverage_missing",
                "typed_identity_mapping_blockers_present",
            ]
            assert {
                (row["facility_id"], row["nm_id"], row["order_count"], row["status_observation_count"])
                for row in blocked_plan["scope"]["typed_blocker_rows"]
            } == {
                ("fac_moscow", 103, 1, 1),
                ("fac_orenburg", 103, 1, 1),
            }
            storage = runner._storage_identity()
            with sqlite3.connect(runtime.db_path) as active_conn:
                active_conn.row_factory = sqlite3.Row
                active = module._active_manifest(active_conn)
            mapping_original = (
                mapping_module.EXPECTED_OPERATIONAL_GENERATION_ID,
                mapping_module.EXPECTED_MANIFEST_SHA256,
                mapping_module.EXPECTED_SQLITE_SCHEMA_VERSION,
                mapping_module.EXPECTED_CUTOVER_ID,
                mapping_module.EXPECTED_TARGET_NM_ID,
                mapping_module.EXPECTED_BLOCKER_CARDINALITY,
            )
            mapping_module.EXPECTED_OPERATIONAL_GENERATION_ID = str(
                storage["operational_generation_id"]
            )
            mapping_module.EXPECTED_MANIFEST_SHA256 = str(storage["manifest_sha256"])
            mapping_module.EXPECTED_SQLITE_SCHEMA_VERSION = int(
                storage["sqlite_schema_version"]
            )
            mapping_module.EXPECTED_CUTOVER_ID = str(active["cutover_id"])
            mapping_module.EXPECTED_TARGET_NM_ID = 103
            mapping_module.EXPECTED_BLOCKER_CARDINALITY = {
                "fac_moscow": {"orders": 1, "statuses": 1},
                "fac_orenburg": {"orders": 1, "statuses": 1},
            }
            try:
                mapping_runner = mapping_module.Wbc0027ExactFbsSkuMappingExtension(
                    runtime_dir=runtime.runtime_dir,
                    deployed_sha=SHA,
                    timestamp_factory=_Clock(),
                )
                mapping_plan = mapping_runner.build_plan(
                    external_identity_digest=module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST
                )
                assert mapping_plan["apply_allowed"] is True, mapping_plan["blockers"]
                rehearsal = mapping_plan["hypothetical_rehearsal"]
                assert rehearsal["accepted"] is True
                assert rehearsal["date_count"] == rehearsal["history_capture_count"] == 15
                assert len(rehearsal["resolved_groups"]) == 4
                _assert_mapping_negative_guards(
                    deployed_sha=SHA,
                    storage=storage,
                    cutover_id=str(active["cutover_id"]),
                    blocked_scope=dict(blocked_plan["scope"]),
                )
                mapping_counts_before = _mapping_non_target_counts(runtime.db_path)
                mapping_evidence = root / "mapping-evidence"
                mapping_evidence.mkdir(mode=0o700)
                mapping_result = mapping_runner.apply(
                    mapping_plan,
                    fingerprint=str(mapping_plan["fingerprint"]),
                    external_identity_digest=module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST,
                    approval_reference="synthetic-mapping-gate",
                    actor="smoke",
                    evidence_dir=mapping_evidence,
                )
                assert mapping_result["status"] == "completed"
                assert mapping_result["mapping_insert_count"] == 1
                assert _mapping_non_target_counts(runtime.db_path) == mapping_counts_before
                repeated_mapping = mapping_runner.apply(
                    mapping_plan,
                    fingerprint=str(mapping_plan["fingerprint"]),
                    external_identity_digest=module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST,
                    approval_reference="synthetic-mapping-gate",
                    actor="smoke",
                    evidence_dir=mapping_evidence,
                )
                assert repeated_mapping["idempotent"] is True
                duplicate = mapping_runner.build_plan(
                    external_identity_digest=module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST
                )
                assert duplicate["apply_allowed"] is False
                assert "active_mapping_count_drift" in duplicate["blockers"]
                assert "duplicate_mapping_present" in duplicate["blockers"]
                synthetic_snapshot = dict(mapping_plan["scope"])
                synthetic_snapshot["typed_blocker_rows"] = []
                assert "typed_blocker_evidence_absent_or_ambiguous" in mapping_module._binding_blockers(
                    deployed_sha=SHA,
                    target_id=mapping_module.CANONICAL_TARGET_ID,
                    external_identity_digest=module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST,
                    storage=storage,
                    source={
                        "cutover_id": active["cutover_id"],
                        "typed_blocker_rows": [],
                        "coverage": blocked_plan["scope"]["coverage"],
                    },
                    identity_snapshot={
                        "tuple_count": 1,
                        "tuple_digest": module.exact_mapping_tuple_digest(),
                        "active_owner_count": 1,
                        "active_mapping_count": 0,
                        "all_mapping_count": 0,
                    },
                )
            finally:
                (
                    mapping_module.EXPECTED_OPERATIONAL_GENERATION_ID,
                    mapping_module.EXPECTED_MANIFEST_SHA256,
                    mapping_module.EXPECTED_SQLITE_SCHEMA_VERSION,
                    mapping_module.EXPECTED_CUTOVER_ID,
                    mapping_module.EXPECTED_TARGET_NM_ID,
                    mapping_module.EXPECTED_BLOCKER_CARDINALITY,
                ) = mapping_original
            plan = runner.build_plan()
            assert plan["apply_allowed"] is True, plan["blockers"]
            assert plan["scope"]["target_count"] == 5
            assert len(plan["scope"]["groups"]) == 4
            assert len(plan["history"]["captures"]) == 15
            assert plan["predicted_effects"]["wb_write_count"] == 0
            history_before = _history_component_count(runtime.db_path)
            result = runner.apply(
                plan,
                fingerprint=str(plan["fingerprint"]),
                approval_reference="synthetic-wbc0027-gate",
                actor="smoke",
                evidence_dir=root / "quality-evidence",
            )
            assert result["status"] == "completed"
            receipt = runner.readback(fingerprint=str(plan["fingerprint"]))
            assert receipt["target_count"] == receipt["target_readback_count"] == 5
            assert receipt["history_capture_count"] == receipt["history_readback_count"] == 15
            repeated = runner.apply(
                plan,
                fingerprint=str(plan["fingerprint"]),
                approval_reference="synthetic-wbc0027-gate",
                actor="smoke",
                evidence_dir=root / "quality-evidence",
            )
            assert repeated["idempotent"] is True
            assert repeated["repeat_submit_performed"] is False
            assert _history_component_count(runtime.db_path) > history_before
            with sqlite3.connect(runtime.db_path) as conn:
                assert conn.execute(
                    f"""SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} pending
                        LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} resolution
                          ON resolution.pending_id=pending.pending_id
                        WHERE resolution.pending_id IS NULL"""
                ).fetchone()[0] == 0
                latest = conn.execute(
                    f"""SELECT component.quantity FROM {FINALIZATIONS_TABLE} finalization
                        JOIN {COMPONENTS_TABLE} component
                          ON component.capture_id=finalization.capture_id
                        WHERE finalization.business_date='2026-08-17'
                          AND component.scope_key='SKU:102'
                          AND component.component_kind='FBS_FACILITY'
                          AND component.component_id='fac_moscow'
                        ORDER BY finalization.finalization_sequence DESC LIMIT 1"""
                ).fetchone()
                assert latest is not None and int(latest[0]) == 99
                for suffix, target_nm in (("a", 101), ("b", 102)):
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_mappings(
                               mapping_id,source_nm_id,source_chrt_id,source_barcode,
                               source_sku,target_nm_id,mapping_digest,active,
                               created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"ambiguous_mapping_{suffix}", 995, 1995, "sku-995",
                            "seller-995", target_nm,
                            "sha256:" + hashlib.sha256(f"ambiguous:{suffix}".encode()).hexdigest(),
                            1, "2026-08-31T13:00:00Z", "smoke",
                        ),
                    )
                _insert_post_t_order(
                    conn,
                    order_id=9800,
                    supplier="new",
                    wb="waiting",
                    source_created_at="2026-08-31T13:01:00Z",
                    observed_at="2026-08-31T13:02:00Z",
                    source_nm_id=995,
                    source_chrt_id=1995,
                    seller_sku="seller-995",
                    barcode="sku-995",
                )
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                ambiguous = process_post_t_fbs_lifecycle(
                    conn,
                    occurred_at="2026-08-31T13:03:00Z",
                    limit=10,
                    schema_ready=True,
                )
                conn.commit()
                assert ambiguous["summary"]["identity_pending"] == 1
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} WHERE order_id=9800"
                ).fetchone()[0] == 1
                assert conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
                    "WHERE order_id=9800"
                ).fetchone()[0] == 0
        finally:
            (
                module.SOURCE_CUTOFF_SEQUENCE,
                module.MOSCOW_FACILITY_ID,
                module.ORENBURG_FACILITY_ID,
                module.TARGET_GROUPS,
                module.TARGET_GROUP_SET,
                module.EXACT_MAPPING_TUPLE,
            ) = original
    print("wbc0027_fbs_lifecycle_quality_recovery_smoke: OK")
    return 0


def _seed_second_facility_and_balances(conn: sqlite3.Connection) -> None:
    now = "2026-08-16T12:00:00Z"
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_ff_facilities VALUES(?,?,?,?,?,?,?)",
        ("fac_orenburg", "ORE", "FF Оренбург", 1, "Asia/Yekaterinburg", now, now),
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_ff_facility_profiles VALUES(?,?,?,?,?)",
        ("fac_orenburg", "Оренбург", "{}", now, now),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings(
               mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
               created_at,created_by) VALUES(?,?,?,?,?,?,?)""",
        ("warehouse_mapping_2", 502, "fac_orenburg", "sha256:" + "7" * 64, 1, now, "smoke"),
    )
    epoch = int(
        conn.execute(
            "SELECT epoch FROM sheet_vitrina_v1_ff_pool_feature_epochs ORDER BY epoch DESC LIMIT 1"
        ).fetchone()[0]
    )
    for facility_id, nm_id in (
        ("fac_moscow", 102),
        ("fac_moscow", 103),
        ("fac_orenburg", 103),
    ):
        conn.execute(
            """INSERT OR REPLACE INTO sheet_vitrina_v1_ff_pool_balances(
                   facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                   wac_rub,source_watermark,updated_at)
               VALUES(?,'FBS',?,?,100,'1000','10','synthetic',?)""",
            (facility_id, nm_id, epoch, now),
        )
    active_version = str(
        conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()[0]
    )
    for nm_id in (102, 103):
        quantity, capital = conn.execute(
            """SELECT SUM(quantity),SUM(CAST(capital_rub AS REAL))
               FROM sheet_vitrina_v1_ff_pool_balances
               WHERE projection_epoch=? AND nm_id=?""",
            (epoch, nm_id),
        ).fetchone()
        quantity = int(quantity)
        capital_text = str(capital)
        existing = conn.execute(
            """SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (active_version, nm_id),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET quantity=?,wac_rub=?,capital_rub=?,cost_covered_quantity=?,
                       quality='exact',certified=1
                   WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                (
                    str(quantity), str(float(capital) / quantity), capital_text,
                    str(quantity), active_version, nm_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                       cost_covered_quantity,quality,certified,wb_quantity,
                       wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    active_version, "ff", nm_id, str(quantity),
                    str(float(capital) / quantity), capital_text, str(quantity),
                    "exact", 1, "0", "0", "0", "{}",
                ),
            )


def _insert_custom_order(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    warehouse_id: int,
    source_nm_id: int,
    source_chrt_id: int,
    seller_sku: str,
    barcode: str,
    source_created_at: str,
    observed_at: str,
) -> None:
    revision = f"post_revision_{order_id}_v1"
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
               observation_id,order_id,source_revision,supply_id,delivery_type,
               source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
               skus_json,observed_at,collector_date_from,collector_date_to,collector_cursor)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"post_observation_{order_id}", order_id, revision, "post-supply", "fbs",
            source_created_at, warehouse_id, 602, source_nm_id, source_chrt_id,
            seller_sku, json.dumps([barcode]), observed_at, 1, 2, 0,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
               evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
               barcode,seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,
               evidence_digest,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"post_identity_evidence_{order_id}", order_id, revision, warehouse_id,
            source_nm_id, source_chrt_id, barcode, seller_sku, "unmatched_identity",
            "warehouse_mapping_2", "",
            "sha256:" + hashlib.sha256(f"identity:{order_id}".encode()).hexdigest(),
            observed_at,
        ),
    )
    _append_status(
        conn,
        order_id=order_id,
        revision=revision,
        supplier_status="new",
        wb_status="waiting",
        episode=1,
        observed_at=observed_at,
        insert_current=True,
    )


def _add_later_canonical_identity(conn: sqlite3.Connection) -> None:
    now = "2026-08-18T00:00:00Z"
    rows = (
        (9600, 999, 1999, "synthetic-unmapped", "synthetic-unmapped", 101, "warehouse_mapping_1"),
        (9701, 998, 1998, "sku-998", "seller-998", 102, "warehouse_mapping_1"),
        (9702, 997, 1997, "sku-997", "seller-997", 103, "warehouse_mapping_1"),
    )
    for order_id, source_nm, chrt_id, barcode, seller_sku, target_nm, warehouse_mapping in rows:
        mapping_id = f"identity_mapping_later_{order_id}"
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_mappings(
                   mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,
                   target_nm_id,mapping_digest,active,created_at,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                mapping_id, source_nm, chrt_id, barcode, seller_sku, target_nm,
                "sha256:" + hashlib.sha256(mapping_id.encode()).hexdigest(), 1, now, "smoke",
            ),
        )
        source = conn.execute(
            """SELECT source_revision,warehouse_id,observed_at
               FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
               WHERE order_id=? ORDER BY observation_sequence DESC LIMIT 1""",
            (order_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
                   evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                   barcode,seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,
                   evidence_digest,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"later_identity_{order_id}", order_id, str(source[0]), int(source[1]),
                source_nm, chrt_id, barcode, seller_sku, "matched", warehouse_mapping,
                mapping_id,
                "sha256:" + hashlib.sha256(f"later:{order_id}".encode()).hexdigest(),
                now,
            ),
        )


def _seed_mapping_owner(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_nomenclature_items)"
        ).fetchall()
    }
    values = {
        "item_id": "synthetic-mapping-owner-996",
        "nm_id": 103,
        "vendor_code": "seller-996",
        "barcode": "sku-996",
        "barcodes_json": '["sku-996"]',
        "is_active": 1,
        "is_hidden": 0,
        "created_at": "2026-08-18T00:00:00Z",
        "updated_at": "2026-08-18T00:00:00Z",
        "source": "smoke",
        "title": "Synthetic mapping owner",
        "nomenclature_name": "Synthetic mapping owner",
        "product_type": "synthetic",
        "match_key": "synthetic|996",
        "aliases_json": "[]",
    }
    selected = [column for column in values if column in columns]
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_nomenclature_items("
        + ",".join(selected)
        + ") VALUES("
        + ",".join("?" for _ in selected)
        + ")",
        tuple(values[column] for column in selected),
    )


def _seed_history(conn: sqlite3.Connection) -> None:
    roster = [
        {
            "facility_id": "fac_moscow", "code": "MSK", "name": "FF Москва",
            "active": True, "applicable": True, "effective_from": "2026-08-14",
            "display_order": 1,
        },
        {
            "facility_id": "fac_orenburg", "code": "ORE", "name": "FF Оренбург",
            "active": True, "applicable": True, "effective_from": "2026-08-17",
            "display_order": 2,
        },
    ]
    for day in range(17, 32):
        business_date = f"2026-08-{day:02d}"
        components = []
        for scope_key, nm_id in (("TOTAL", None), ("SKU:101", 101), ("SKU:102", 102), ("SKU:103", 103)):
            scope_kind = "TOTAL" if nm_id is None else "SKU"
            components.append(
                _component(scope_kind, scope_key, nm_id, "WB", "WB", "WB", 50)
            )
            components.append(
                _component(scope_kind, scope_key, nm_id, "FBS_FACILITY", "fac_moscow", "FF Москва", 300 if nm_id is None else 100)
            )
            components.append(
                _component(scope_kind, scope_key, nm_id, "FBS_FACILITY", "fac_orenburg", "FF Оренбург", 300 if nm_id is None else 100)
            )
        capture = append_inventory_history_capture(
            conn,
            business_date=business_date,
            capture_kind="historical_backfill",
            formula_version="inventory_planning_v1",
            facility_roster=roster,
            source_manifest={"contract": "synthetic_exact_same_date", "date": business_date},
            components=components,
            captured_at=f"{business_date}T20:00:00Z",
        )
        if day < 31:
            append_inventory_history_finalization(
                conn,
                business_date=business_date,
                capture_id=str(capture["capture_id"]),
                finalization_identity=f"synthetic:{business_date}",
                finalized_at=f"{business_date}T21:00:00Z",
                provenance={"source": "smoke"},
            )


def _component(
    scope_kind: str,
    scope_key: str,
    nm_id: int | None,
    component_kind: str,
    component_id: str,
    label: str,
    quantity: int,
) -> dict[str, object]:
    return {
        "scope_kind": scope_kind,
        "scope_key": scope_key,
        "nm_id": nm_id,
        "component_kind": component_kind,
        "component_id": component_id,
        "component_label": label,
        "state": "exact_zero" if quantity == 0 else "exact",
        "quantity": quantity,
        "source_revision": "synthetic",
        "source_digest": "sha256:" + "a" * 64,
        "source_watermark": "synthetic",
        "provenance": {"source": "smoke"},
    }


def _history_component_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {COMPONENTS_TABLE}").fetchone()[0])


def _mapping_non_target_counts(path: Path) -> dict[str, int]:
    tables = (
        "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events",
        IDENTITY_PENDING_RESOLUTIONS_TABLE,
        "sheet_vitrina_v1_ff_pool_fbs_quality_recovery_runs",
        "sheet_vitrina_v1_ff_pool_fbs_quality_recovery_targets",
        "sheet_vitrina_v1_ff_pool_fbs_quality_recovery_history",
        COMPONENTS_TABLE,
        FINALIZATIONS_TABLE,
    )
    with sqlite3.connect(path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def _assert_mapping_negative_guards(
    *,
    deployed_sha: str,
    storage: dict[str, object],
    cutover_id: str,
    blocked_scope: dict[str, object],
) -> None:
    source = {
        "cutover_id": cutover_id,
        "typed_blocker_rows": list(blocked_scope["typed_blocker_rows"]),
        "coverage": dict(blocked_scope["coverage"]),
    }
    identity = {
        "tuple_count": 1,
        "tuple_digest": module.exact_mapping_tuple_digest(),
        "active_owner_count": 1,
        "active_mapping_count": 0,
        "all_mapping_count": 0,
    }

    def codes(
        *,
        digest: str = module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST,
        storage_value: dict[str, object] | None = None,
        source_value: dict[str, object] | None = None,
        identity_value: dict[str, object] | None = None,
    ) -> set[str]:
        return set(
            mapping_module._binding_blockers(
                deployed_sha=deployed_sha,
                target_id=mapping_module.CANONICAL_TARGET_ID,
                external_identity_digest=digest,
                storage=storage_value or storage,
                source=source_value or source,
                identity_snapshot=identity_value or identity,
            )
        )

    assert "external_identity_digest_drift" in codes(digest="sha256:" + "0" * 64)
    changed = dict(storage)
    changed["operational_generation_id"] = "foreign-generation"
    assert "storage_generation_drift" in codes(storage_value=changed)
    changed = dict(storage)
    changed["sqlite_schema_version"] = int(storage["sqlite_schema_version"]) + 1
    assert "storage_schema_revision_drift" in codes(storage_value=changed)
    changed_source = dict(source)
    changed_source["cutover_id"] = "foreign-cutover"
    assert "cutover_drift" in codes(source_value=changed_source)
    for field, value, expected in (
        ("tuple_count", 0, "tuple_count_drift"),
        ("active_owner_count", 2, "owner_count_drift"),
        ("active_mapping_count", 1, "active_mapping_count_drift"),
        ("all_mapping_count", 1, "duplicate_mapping_present"),
    ):
        changed_identity = dict(identity)
        changed_identity[field] = value
        assert expected in codes(identity_value=changed_identity)
    foreign_source = dict(source)
    foreign_rows = [dict(row) for row in source["typed_blocker_rows"]]
    foreign_rows[0]["facility_id"] = "foreign-facility"
    foreign_rows[0]["nm_id"] = 999
    foreign_source["typed_blocker_rows"] = foreign_rows
    assert "typed_blocker_scope_or_cardinality_drift" in codes(
        source_value=foreign_source
    )
    absent_source = dict(source)
    absent_source["typed_blocker_rows"] = []
    assert "typed_blocker_evidence_absent_or_ambiguous" in codes(
        source_value=absent_source
    )
    previous_target = mapping_module.EXPECTED_TARGET_NM_ID
    mapping_module.EXPECTED_TARGET_NM_ID = 104
    try:
        assert "target_nm_drift" in codes()
    finally:
        mapping_module.EXPECTED_TARGET_NM_ID = previous_target


if __name__ == "__main__":
    raise SystemExit(main())
