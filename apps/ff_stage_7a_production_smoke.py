"""Smoke checks for the bounded owner-gated Stage 7A production runner."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_fbs_orders import (  # noqa: E402
    WbFbsOffice,
    WbFbsOrderStatus,
    WbFbsOrdersPage,
    WbFbsSellerWarehouse,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.ff_stage_7a_production import (  # noqa: E402
    ENV_KEY,
    FfStage7AProductionMutation,
    MOSCOW_NAME,
    ORENBURG_NAME,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)


DEPLOYED_SHA = "a" * 40
NOW_UNIX = 1_786_592_400


class _Source:
    def __init__(self, *, fail_on_order_call: int | None = None) -> None:
        self.order_calls = 0
        self.status_calls = 0
        self.fail_on_order_call = fail_on_order_call

    def list_seller_warehouses(self) -> list[WbFbsSellerWarehouse]:
        return [
            WbFbsSellerWarehouse(
                warehouse_id=1_988_668,
                office_id=14_017,
                name="ЕФ Быково",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=854_205,
                office_id=12_223,
                name="FBS склад",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
        ]

    def list_offices(self) -> list[WbFbsOffice]:
        return [
            WbFbsOffice(
                office_id=14_017,
                name="Москва (Софьино)",
                city="Москва_Восток",
                federal_district="Центральный федеральный округ",
            ),
            WbFbsOffice(
                office_id=12_223,
                name="Orenburg Central",
                city="Оренбург",
                federal_district="Приволжский федеральный округ",
            ),
        ]

    def list_orders(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int | None,
        date_to: int | None,
    ) -> WbFbsOrdersPage:
        self.order_calls += 1
        if self.order_calls == self.fail_on_order_call:
            raise RuntimeError("simulated official read interruption")
        assert next_cursor == 0
        return WbFbsOrdersPage(
            orders=[
                _order(1001, nm_id=101, chrt_id=10001, barcode="46001", sku="SKU-101"),
                _order(1002, nm_id=102, chrt_id=10002, barcode="46002", sku="SKU-102"),
                _order(1003, nm_id=103, chrt_id=0, barcode="46003", sku="SKU-103"),
                _order(1004, nm_id=104, chrt_id=10004, barcode="46004", sku="SKU-104"),
                _order(9999, nm_id=999, chrt_id=999, barcode="bad", sku="bad", delivery_type="wbgo"),
            ],
            next_cursor=0,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )

    def list_statuses(self, order_ids: list[int]) -> list[WbFbsOrderStatus]:
        self.status_calls += 1
        return [
            WbFbsOrderStatus(
                order_id=order_id,
                supplier_status="complete",
                wb_status="waiting",
            )
            for order_id in order_ids
        ]


def main() -> None:
    with TemporaryDirectory(prefix="ff-stage-7a-production-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        _seed_nomenclature(runtime.db_path)
        env_file = root / "runtime.env"
        env_file.write_text(f"{ENV_KEY}=false\n", encoding="utf-8")
        env_file.chmod(0o600)
        source = _Source(fail_on_order_call=4)
        mutation = FfStage7AProductionMutation(
            runtime_dir=runtime_dir,
            env_file=env_file,
            deployed_sha=DEPLOYED_SHA,
            timestamp_factory=lambda: "2026-08-13T12:00:00Z",
            unix_time_factory=lambda: NOW_UNIX,
            source=source,
        )

        plan = mutation.build_plan(watermark_unix=NOW_UNIX)
        assert plan["apply_allowed"] and not plan["blockers"]
        assert plan["official_order_preview"]["order_count"] == 4
        assert plan["expected_effects"]["facility_insert_count"] == 2
        assert plan["expected_effects"]["warehouse_mapping_insert_count"] == 1
        assert plan["expected_effects"]["identity_mapping_insert_count"] == 1
        mappings = plan["exact_mappings"]
        assert mappings["unrouted_warehouse_observation_count"] == 0
        assert mappings["unmatched_identity_observation_count"] == 1
        assert mappings["deferred_identity_observation_count"] == 1
        assert mappings["ambiguous_identity_observation_count"] == 1
        assert mappings["unrouted_facility_names"] == [ORENBURG_NAME]
        assert [row["seller_warehouse_id"] for row in mappings["warehouse"]] == [1_988_668]
        assert mappings["warehouse"][0]["official_office_id"] == 14_017

        try:
            mutation.apply(
                plan,
                fingerprint=plan["fingerprint"],
                approval_reference="github-pr-123#issuecomment-456",
                actor="owner",
                backup_dir=root / "backups",
            )
            raise AssertionError("interrupted official collection must fail safely")
        except RuntimeError as exc:
            assert "simulated official read interruption" in str(exc)
        assert env_file.read_text(encoding="utf-8").splitlines() == [f"{ENV_KEY}=false"]

        source = _Source()
        mutation = FfStage7AProductionMutation(
            runtime_dir=runtime_dir,
            env_file=env_file,
            deployed_sha=DEPLOYED_SHA,
            timestamp_factory=lambda: "2026-08-13T12:00:00Z",
            unix_time_factory=lambda: NOW_UNIX,
            source=source,
        )
        result = mutation.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="github-pr-123#issuecomment-456",
            actor="owner",
            backup_dir=root / "backups",
        )
        assert result["status"] == "complete"
        assert result["catchup"]["complete"]
        assert result["next_collection_probe"]["complete"]
        assert result["backup"]["kind"] == "exact_target_before_image"
        assert result["backup"]["integrity_check"] == "sha256_verified"
        assert result["backup"]["resumed"] is True
        assert result["reconciliation"]["collector_configuration"]["enabled"] is True
        assert result["reconciliation"]["official_orders"]["observation_count"] == 4
        assert result["reconciliation"]["official_orders"]["status_observation_count"] == 4
        assert result["reconciliation"]["official_orders"]["earliest_official_order_date"] == "2026-08-12"
        assert result["reconciliation"]["mappings"]["matched_count"] == 1
        assert result["reconciliation"]["mappings"]["unmatched_count"] == 2
        assert result["reconciliation"]["mappings"]["deferred_count"] == 1
        assert result["reconciliation"]["non_target_invariants"] == plan["non_target_invariants"]
        _assert_database(runtime.db_path)

        repeated = mutation.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="github-pr-123#issuecomment-456",
            actor="owner",
            backup_dir=root / "backups",
        )
        assert repeated["idempotent"] is True
        assert len(list((root / "backups").glob("*.before.json"))) == 1
        assert env_file.read_text(encoding="utf-8").splitlines() == [f"{ENV_KEY}=true"]
        assert source.status_calls == 2
    print("ff_stage_7a_production_smoke: OK")


def _seed_nomenclature(db_path: Path) -> None:
    now = "2026-08-13T11:00:00Z"
    rows = [
        ("item-101", 101, "46001", "SKU-101"),
        ("item-104-a", 104, "46004", "SKU-104"),
        ("item-104-b", 104, "46004", "SKU-104"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO sheet_vitrina_v1_nomenclature_items(
                   item_id,is_active,is_hidden,nm_id,barcode,barcodes_json,
                   barcode_source,barcode_status,vendor_code,nomenclature_name,
                   product_type,match_key,aliases_json,created_at,updated_at
               ) VALUES(?,1,0,?,?,?,'fixture','active',?,?,?,?,'[]',?,?)""",
            [
                (item_id, nm_id, barcode, f'["{barcode}"]', sku, sku, "fixture", sku, now, now)
                for item_id, nm_id, barcode, sku in rows
            ],
        )
        conn.commit()


def _assert_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        facilities = conn.execute(
            f"SELECT name,active FROM {FACILITIES_TABLE} ORDER BY name"
        ).fetchall()
        assert facilities == [(MOSCOW_NAME, 1), (ORENBURG_NAME, 0)]
        assert conn.execute(f"SELECT COUNT(*) FROM {WAREHOUSE_MAPPINGS_TABLE}").fetchone()[0] == 1
        assert conn.execute(f"SELECT COUNT(*) FROM {IDENTITY_MAPPINGS_TABLE}").fetchone()[0] == 1
        outcomes = dict(
            conn.execute(
                f"SELECT outcome,COUNT(*) FROM {IDENTITY_EVIDENCE_TABLE} GROUP BY outcome"
            ).fetchall()
        )
        assert outcomes == {"deferred": 1, "matched": 1, "unmatched_identity": 2}
        assert conn.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS_TABLE}").fetchone()[0] == 4
        assert conn.execute(f"SELECT COUNT(*) FROM {STATUS_OBSERVATIONS_TABLE}").fetchone()[0] == 4
        assert conn.execute(f"SELECT COUNT(*) FROM {FEATURE_EPOCHS_TABLE}").fetchone()[0] == 0
        for table in (
            "sheet_vitrina_v1_warehouse_business_operations",
            "sheet_vitrina_v1_ff_pool_movement_lines",
            "sheet_vitrina_v1_ff_stock_operations",
            "sheet_vitrina_v1_ff_stock_reservation_operations",
        ):
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if present:
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def _order(
    order_id: int,
    *,
    nm_id: int,
    chrt_id: int,
    barcode: str,
    sku: str,
    delivery_type: str = "fbs",
) -> dict[str, object]:
    return {
        "id": order_id,
        "deliveryType": delivery_type,
        "createdAt": "2026-08-12T08:00:00Z",
        "warehouseId": 1_988_668,
        "officeId": 14_017,
        "nmId": nm_id,
        "chrtId": chrt_id,
        "article": sku,
        "skus": [barcode],
        "cargoType": 1,
        "crossBorderType": 0,
        "isZeroOrder": False,
    }


if __name__ == "__main__":
    main()
