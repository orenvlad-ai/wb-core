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
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            processed = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-17T04:00:00Z",
                limit=100,
                schema_ready=True,
            )
            conn.commit()
            assert processed["summary"]["identity_pending"] == 3
            _add_later_canonical_identity(conn)
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
            ).fetchone()[0] == 4
            coverage = fbs_lifecycle_quality_coverage(
                conn,
                as_of_date="2026-08-17",
                requested_nm_ids={101, 102, 103},
            )
            assert coverage["status"] == "partial"
            assert {
                (str(item["facility_id"]), int(item["nm_id"]))
                for item in coverage["groups"]
            } == {
                ("fac_moscow", 101),
                ("fac_moscow", 102),
                ("fac_moscow", 103),
                ("fac_orenburg", 103),
            }
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
        try:
            runner = module.Wbc0027FbsLifecycleQualityRecovery(
                runtime_dir=runtime.runtime_dir,
                deployed_sha=SHA,
                timestamp_factory=_Clock(),
            )
            plan = runner.build_plan()
            assert plan["apply_allowed"] is True, plan["blockers"]
            assert plan["scope"]["target_count"] == 4
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
            assert receipt["target_count"] == receipt["target_readback_count"] == 4
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
        (9703, 996, 1996, "sku-996", "seller-996", 103, "warehouse_mapping_2"),
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


if __name__ == "__main__":
    raise SystemExit(main())
