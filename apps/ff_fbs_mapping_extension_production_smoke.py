#!/usr/bin/env python3
"""Production-shaped exact Orenburg mapping, pending replay and retry smoke."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_cutover_production_smoke import (  # noqa: E402
    SHA as CUTOVER_SHA,
    SHIPMENT_ID,
    _Clock,
    _barrier,
    _seed,
)
from packages.adapters.wb_fbs_orders import (  # noqa: E402
    WbFbsOffice,
    WbFbsSellerWarehouse,
)
from packages.application.ff_fbs_mapping_extension_production import (  # noqa: E402
    EXPECTED_RECEIPT_CAPITAL_RUB,
    EXPECTED_RECEIPT_QUANTITY,
    FfFbsMappingExtensionProductionMutation,
    MOSCOW_FACILITY_ID,
    MOSCOW_WAREHOUSE_ID,
    RECEIPT_DOCUMENT_ID,
    RECEIPT_ROOT_DOCUMENT_ID,
    TARGET_FACILITY_ID,
    TARGET_OFFICE_CITY,
    TARGET_OFFICE_ID,
    TARGET_OFFICE_NAME,
    TARGET_WAREHOUSE_ID,
    TARGET_WAREHOUSE_NAME,
)
from packages.application.ff_pool_cutover_production import (  # noqa: E402
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


DEPLOYED_SHA = "d" * 40
NOW = "2026-08-20T06:00:00Z"


@dataclass
class _OfficialSource:
    def list_seller_warehouses(self) -> list[WbFbsSellerWarehouse]:
        return [
            WbFbsSellerWarehouse(
                warehouse_id=TARGET_WAREHOUSE_ID,
                office_id=TARGET_OFFICE_ID,
                name=TARGET_WAREHOUSE_NAME,
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=MOSCOW_WAREHOUSE_ID,
                office_id=14017,
                name="ЕФ Быково",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
        ]

    def list_offices(self) -> list[WbFbsOffice]:
        return [
            WbFbsOffice(
                office_id=TARGET_OFFICE_ID,
                name=TARGET_OFFICE_NAME,
                city=TARGET_OFFICE_CITY,
                federal_district="Приволжский федеральный округ",
            ),
            WbFbsOffice(
                office_id=14017,
                name="Москва (Софьино)",
                city="Москва_Восток",
                federal_district="Центральный федеральный округ",
            ),
        ]


def main() -> int:
    with TemporaryDirectory(prefix="ff-fbs-mapping-extension-") as tmp:
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
        cutover = FfPoolCutoverProductionMutation(
            runtime_dir=runtime_dir,
            env_file=env_file,
            deployed_sha=CUTOVER_SHA,
            timestamp_factory=_Clock(),
        )
        gate = cutover.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
        applied = cutover.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-stage7c-smoke",
            actor="smoke",
            backup_dir=root / "cutover-backups",
            external_barrier_evidence=_barrier(),
        )
        assert applied["status"] == "applied_reconciled"

        _seed_orenburg(runtime)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            pending = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-20T05:59:00Z",
            )
            conn.commit()
        assert pending["summary"]["identity_pending"] == 4
        assert pending["summary"]["late_pre_t"] == 1

        runner = FfFbsMappingExtensionProductionMutation(
            runtime_dir=runtime_dir,
            deployed_sha=DEPLOYED_SHA,
            timestamp_factory=lambda: NOW,
            source=_OfficialSource(),
        )
        plan = runner.build_plan()
        assert plan["apply_allowed"] is True, plan["blockers"]
        assert plan["source"]["frozen_backlog"]["order_count"] == 4
        assert plan["source"]["frozen_backlog"]["status_count"] == 5
        assert plan["source"]["accounting_boundary"][
            "post_watermark_growth_invalidates_gate"
        ] is False
        assert plan["expected_effects"]["frozen_expected_final_reserved_count"] == 1
        assert plan["expected_effects"]["frozen_expected_final_fulfilled_count"] == 1
        assert plan["expected_effects"][
            "frozen_expected_final_cancelled_or_released_count"
        ] == 1
        assert plan["expected_effects"]["frozen_expected_late_noop_count"] == 1
        result = runner.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="owner-apply-gate-smoke",
            actor="smoke",
            evidence_dir=root / "mapping-evidence",
        )
        assert result["status"] == "complete"
        readback = result["readback"]
        assert readback["status"] == "ready", readback["blockers"]
        assert readback["backlog_partition"]["reserved_count"] == 1
        assert readback["backlog_partition"]["fulfilled_count"] == 1
        assert readback["backlog_partition"]["frozen_cancelled_or_released_count"] == 1
        assert readback["backlog_partition"]["frozen_late_noop_count"] == 1
        assert readback["backlog_partition"]["frozen_unresolved_pending_count"] == 0
        assert readback["pool_aggregate_parity"]["status"] == "pass"
        assert readback["wb_writes"] == 0
        assert Path(result["before_image"]["path"]).stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_order(
                conn,
                order_id=880003,
                nm_id=1999,
                supplier="new",
                wb="waiting",
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_mappings(
                       mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,
                       target_nm_id,mapping_digest,active,created_at,created_by
                   ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
                (
                    "post-w-unallocated-identity", 1999, 51999, "barcode-1999",
                    "seller-1999", 1999, "sha256:" + "6" * 64, NOW, "smoke",
                ),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
                       evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                       barcode,seller_sku,outcome,warehouse_mapping_id,
                       identity_mapping_id,evidence_digest,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "post-w-unallocated-evidence", 880003,
                    "orenburg-revision-880003", TARGET_WAREHOUSE_ID, 1999, 51999,
                    "barcode-1999", "seller-1999", "matched",
                    result["apply"]["mapping_id"], "post-w-unallocated-identity",
                    "sha256:" + "5" * 64, NOW,
                ),
            )
            suffix = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-20T06:01:00Z",
            )
            conn.commit()
        assert suffix["summary"]["identity_pending"] == 1
        suffix_readback = runner.readback()
        assert suffix_readback["status"] == "ready", suffix_readback["blockers"]
        assert suffix_readback["backlog_partition"]["frozen_unresolved_pending_count"] == 0
        assert suffix_readback["backlog_partition"]["post_w_unresolved_pending_count"] == 1
        repeated = runner.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="owner-apply-gate-smoke",
            actor="smoke",
            evidence_dir=root / "mapping-evidence",
        )
        assert repeated["idempotent"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings "
                "WHERE seller_warehouse_id=?",
                (TARGET_WAREHOUSE_ID,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
                "WHERE facility_id=? AND event_type='handoff_debit'",
                (TARGET_FACILITY_ID,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations "
                "WHERE source_type='fbs_order_lifecycle_event' AND source_id='880002'",
            ).fetchone()[0] == 1
    print("ff_fbs_mapping_extension_production_smoke: OK")
    return 0


def _seed_orenburg(runtime: RegistryUploadDbBackedRuntime) -> None:
    for offset in range(21):
        nm_id = 1001 + offset
        runtime.save_nomenclature_item(
            {
                "item_id": f"orenburg-{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "vendor_code": f"seller-{nm_id}",
                "nm_id": nm_id,
                "barcode": f"barcode-{nm_id}",
                "nomenclature_name": f"Orenburg SKU {nm_id}",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_ff_facilities VALUES(?,?,?,?,?,?,?)",
            (TARGET_FACILITY_ID, "OREN", "FF Оренбург", 1, "Asia/Yekaterinburg", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_ff_facility_profiles VALUES(?,?,?,?,?)",
            (TARGET_FACILITY_ID, TARGET_OFFICE_CITY, "{}", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_ff_facilities VALUES(?,?,?,?,?,?,?)",
            (MOSCOW_FACILITY_ID, "MSK2", "FF Москва canonical", 1, "Asia/Yekaterinburg", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_ff_facility_profiles VALUES(?,?,?,?,?)",
            (MOSCOW_FACILITY_ID, "Москва", "{}", NOW, NOW),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings(
                   mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                   created_at,created_by
               ) VALUES(?,?,?,?,1,?,?)""",
            ("moscow-official-smoke", MOSCOW_WAREHOUSE_ID, MOSCOW_FACILITY_ID, "sha256:" + "9" * 64, NOW, "smoke"),
        )
        _seed_receipt(conn)
        feature_epoch = conn.execute(
            "SELECT MAX(epoch) FROM sheet_vitrina_v1_ff_pool_feature_epochs"
        ).fetchone()[0]
        version_id = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()[0]
        for offset in range(21):
            nm_id = 1001 + offset
            quantity = 1000 if offset < 20 else 6750
            capital = Decimal("100000") if offset < 20 else Decimal("874226.82")
            wac = capital / Decimal(quantity)
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ff_pool_balances(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    TARGET_FACILITY_ID, "FBS", nm_id, feature_epoch, quantity,
                    (
                        format(capital, ".2f")
                        if offset == 0
                        else format(capital, "f")
                    ),
                    format(wac, "f"), RECEIPT_DOCUMENT_ID, NOW,
                ),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                       cost_covered_quantity,quality,certified,wb_quantity,
                       wb_in_way_to_client,wb_in_way_from_client,provenance_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id, "ff", nm_id, str(quantity), format(wac, "f"),
                    format(capital, "f"), str(quantity), "exact", 1, "0", "0", "0", "{}",
                ),
            )
        _insert_order(
            conn,
            order_id=880000,
            nm_id=1003,
            supplier="new",
            wb="waiting",
            observed_at="2026-08-13T05:51:00Z",
            status_observed_at="2026-08-13T05:52:00Z",
        )
        _insert_order(conn, order_id=880001, nm_id=1001, supplier="new", wb="waiting")
        _insert_order(conn, order_id=880002, nm_id=1002, supplier="complete", wb="sorted")
        _insert_order(conn, order_id=880004, nm_id=1004, supplier="new", wb="waiting")
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
                   observation_id,order_id,order_revision,status_digest,supplier_status,
                   wb_status,positive_quantity,observed_at
               ) VALUES(?,?,?,?,?,?,1,?)""",
            (
                "orenburg-status-880004-cancel",
                880004,
                "orenburg-revision-880004",
                "sha256:" + "4" * 64,
                "cancel",
                "canceled_by_client",
                "2026-08-20T05:53:00Z",
            ),
        )
        conn.execute(
            """UPDATE sheet_vitrina_v1_wb_supplies_fbs_collector_state
               SET last_status='success',last_error='',complete=1,next_cursor=0,
                   last_attempt_at=?,last_success_at=?,window_date_to=?
               WHERE state_id=1""",
            (NOW, NOW, 2_000_000_000),
        )
        conn.commit()


def _seed_receipt(conn: sqlite3.Connection) -> None:
    for document_id, kind, root, operation_id, role in (
        (RECEIPT_ROOT_DOCUMENT_ID, "transfer_root", RECEIPT_ROOT_DOCUMENT_ID, "op-root", "primary"),
        (RECEIPT_DOCUMENT_ID, "transfer_receipt", RECEIPT_ROOT_DOCUMENT_ID, "op-receipt", "receipt"),
    ):
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ff_pool_documents(
                   document_id,request_id,document_role,document_kind,root_document_id,
                   operation_id,source_system,source_type,source_id,source_revision,
                   idempotency_epoch,actor,business_date,posted_manifest_sha256,
                   posted_manifest_json,posted_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id, "request-" + role, role, kind, root, operation_id,
                "smoke", kind, document_id, "revision-" + role, 1, "smoke",
                "2026-08-20", "sha256:" + ("7" if role == "primary" else "8") * 64,
                "{}", NOW,
            ),
        )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_ff_pool_document_relations(
               parent_document_id,child_document_id,root_document_id,relation_type,created_at
           ) VALUES(?,?,?,'receipt_of',?)""",
        (RECEIPT_ROOT_DOCUMENT_ID, RECEIPT_DOCUMENT_ID, RECEIPT_ROOT_DOCUMENT_ID, NOW),
    )
    for offset in range(21):
        nm_id = 1001 + offset
        quantity = 1000 if offset < 20 else 6750
        capital = "100000" if offset < 20 else "874226.82"
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ff_pool_document_lines(
                   document_id,line_no,root_document_id,line_role,facility_id,pool,
                   nm_id,quantity,capital_rub,expense_rub,metadata_json
               ) VALUES(?,?,?,?,?,'FBS',?,?,?,'0','{}')""",
            (
                RECEIPT_DOCUMENT_ID, offset + 1, RECEIPT_ROOT_DOCUMENT_ID,
                "receipt", TARGET_FACILITY_ID, nm_id, quantity, capital,
            ),
        )
    assert sum(1000 if offset < 20 else 6750 for offset in range(21)) == EXPECTED_RECEIPT_QUANTITY
    assert Decimal("100000") * 20 + Decimal("874226.82") == Decimal(EXPECTED_RECEIPT_CAPITAL_RUB)


def _insert_order(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    nm_id: int,
    supplier: str,
    wb: str,
    observed_at: str = "2026-08-20T05:51:00Z",
    status_observed_at: str = "2026-08-20T05:52:00Z",
) -> None:
    revision = f"orenburg-revision-{order_id}"
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
               observation_id,order_id,source_revision,supply_id,delivery_type,
               source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
               skus_json,observed_at,collector_date_from,collector_date_to,collector_cursor
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"orenburg-observation-{order_id}", order_id, revision, "orenburg-supply",
            "fbs", observed_at, TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID,
            nm_id, nm_id + 50_000, f"seller-{nm_id}", f'["barcode-{nm_id}"]',
            observed_at, 1, 2_000_000_000, 0,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
               observation_id,order_id,order_revision,status_digest,supplier_status,
               wb_status,positive_quantity,observed_at
           ) VALUES(?,?,?,?,?,?,1,?)""",
        (
            f"orenburg-status-{order_id}", order_id, revision,
            "sha256:" + str(order_id).zfill(64), supplier, wb,
            status_observed_at,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
