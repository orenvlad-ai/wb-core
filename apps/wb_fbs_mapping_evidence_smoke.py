#!/usr/bin/env python3
"""Regression proof for one append-only legacy FBS mapping evidence upgrade."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_content import (  # noqa: E402
    WbContentCard,
    WbContentCatalogSnapshot,
)
from packages.adapters.wb_fbs_orders import (  # noqa: E402
    WbFbsOffice,
    WbFbsSellerWarehouse,
    WbFbsStock,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    ensure_ff_pool_foundation_schema,
)
from packages.application.wb_fbs_mapping_evidence import (  # noqa: E402
    MAPPING_EVIDENCE_VERSIONS_TABLE,
    RESTORE_MODE,
    UPGRADE_MODE,
    WbFbsMappingEvidenceError,
    WbFbsMappingEvidenceUpgrade,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    WAREHOUSE_MAPPINGS_TABLE,
    ensure_wb_fbs_orders_schema,
)
from packages.application.wb_fbs_warehouse_registry import (  # noqa: E402
    WbFbsWarehouseRegistry,
    ensure_wb_fbs_warehouse_registry_schema,
)


MOSCOW_WAREHOUSE_ID = 7_001
MOSCOW_OFFICE_ID = 501
MOSCOW_FACILITY_ID = "fac_moscow"
MOSCOW_MAPPING_ID = "map_moscow_legacy"
MOSCOW_MAPPING_DIGEST = "sha256:" + "1" * 64
ORENBURG_WAREHOUSE_ID = 7_002
ORENBURG_OFFICE_ID = 502
ORENBURG_FACILITY_ID = "fac_orenburg"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        current = self.value
        self.value += timedelta(seconds=1)
        return current.isoformat().replace("+00:00", "Z")


class OfficialSource:
    def list_seller_warehouses(self):
        return [
            WbFbsSellerWarehouse(
                warehouse_id=ORENBURG_WAREHOUSE_ID,
                office_id=ORENBURG_OFFICE_ID,
                name="Official Orenburg warehouse",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=MOSCOW_WAREHOUSE_ID,
                office_id=MOSCOW_OFFICE_ID,
                name="Official Moscow warehouse",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
        ]

    def list_offices(self):
        return [
            WbFbsOffice(
                office_id=ORENBURG_OFFICE_ID,
                name="Official Orenburg office",
                city="Orenburg",
                federal_district="Fixture district B",
            ),
            WbFbsOffice(
                office_id=MOSCOW_OFFICE_ID,
                name="Official Moscow office",
                city="Moscow",
                federal_district="Fixture district A",
            ),
        ]

    def list_stocks(self, *, warehouse_id, chrt_ids):
        assert chrt_ids == [9001, 9002]
        return [
            WbFbsStock(chrt_id=9001, amount=3 if warehouse_id == MOSCOW_WAREHOUSE_ID else 2)
        ]


class OfficeDriftSource(OfficialSource):
    def __init__(self) -> None:
        self.calls = 0

    def list_seller_warehouses(self):
        self.calls += 1
        rows = super().list_seller_warehouses()
        if self.calls < 2:
            return rows
        return [
            WbFbsSellerWarehouse(
                warehouse_id=row.warehouse_id,
                office_id=(503 if row.warehouse_id == MOSCOW_WAREHOUSE_ID else row.office_id),
                name=row.name,
                cargo_type=row.cargo_type,
                delivery_type=row.delivery_type,
                is_deleting=row.is_deleting,
                is_processing=row.is_processing,
            )
            for row in rows
        ]

    def list_offices(self):
        return super().list_offices() + [
            WbFbsOffice(
                office_id=503,
                name="Changed office",
                city="Moscow",
                federal_district="Fixture district A",
            )
        ]


class CatalogSource:
    def fetch_catalog_snapshot(self):
        return WbContentCatalogSnapshot.from_cards(
            [
                WbContentCard(
                    nm_id=101,
                    vendor_code="SKU-101",
                    title="SKU 101",
                    subject_name="Fixture",
                    updated_at="2026-09-05T11:00:00Z",
                    barcodes=["bc-101"],
                    chrt_ids=[9001],
                ),
                WbContentCard(
                    nm_id=202,
                    vendor_code="SKU-202",
                    title="SKU 202",
                    subject_name="Fixture",
                    updated_at="2026-09-05T11:00:01Z",
                    barcodes=["bc-202"],
                    chrt_ids=[9002],
                ),
            ],
            pages_fetched=1,
            terminal_short_page=True,
            cursor_chain_digest="sha256:fixture-cursor-chain",
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wb-fbs-mapping-evidence-") as raw:
        path = Path(raw) / "operational.sqlite3"
        _seed(path)
        clock = Clock()
        source = OfficialSource()
        catalog = CatalogSource()
        registry = WbFbsWarehouseRegistry(
            db_path=path,
            timestamp_factory=clock,
            source=source,
            catalog_source=catalog,
        )
        before_generation = registry.collect()
        assert before_generation["latest_attempt"]["complete"] == 0
        assert before_generation["source_generation"]["complete"] is False
        assert before_generation["latest_attempt"]["error"] == (
            "active exact warehouse mapping scope incomplete"
        )

        request = {
            "mode": UPGRADE_MODE,
            "mapping_id": MOSCOW_MAPPING_ID,
            "seller_warehouse_id": MOSCOW_WAREHOUSE_ID,
            "facility_id": MOSCOW_FACILITY_ID,
            "expected_mapping_digest": MOSCOW_MAPPING_DIGEST,
            "expected_office_id": MOSCOW_OFFICE_ID,
        }
        service = WbFbsMappingEvidenceUpgrade(
            db_path=path,
            storage_identity={
                "generation_id": "operational-fixture",
                "generation_epoch": "fixture",
                "manifest_sha256": "sha256:" + "a" * 64,
            },
            source=source,
            timestamp_factory=clock,
            actor="fixture",
        )
        not_submitted = service.readback(request, "wbc-0054-moscow-evidence-v1")
        assert not_submitted["state"] == "not_submitted"
        preview = service.preview(request, "wbc-0054-moscow-evidence-v1")
        assert preview["scope"] == {
            "mode": UPGRADE_MODE,
            "mapping_id": MOSCOW_MAPPING_ID,
            "seller_warehouse_id": MOSCOW_WAREHOUSE_ID,
            "facility_id": MOSCOW_FACILITY_ID,
            "official_office_id": MOSCOW_OFFICE_ID,
            "mapping_row_count": 1,
        }
        assert preview["effect"] == {
            "append_mapping_evidence_version_count": 1,
            "update_or_delete_mapping_count": 0,
            "facility_create_count": 0,
            "inventory_or_movement_count": 0,
            "cost_or_lifecycle_event_count": 0,
            "wb_write_count": 0,
        }
        before = _protected_image(path)
        submitted = service.apply(
            request,
            "wbc-0054-moscow-evidence-v1",
            expected_prestate=preview["prestate_sha256"],
            expected_candidate=preview["candidate_sha256"],
        )
        assert submitted["disposition"] == "submitted"
        assert submitted["row_insert_count"] == 1
        applied = service.readback(request, "wbc-0054-moscow-evidence-v1")
        assert applied["state"] == "applied"
        assert applied["official_office_id"] == MOSCOW_OFFICE_ID
        assert applied["row_insert_count"] == 1
        repeat = service.apply(
            request,
            "wbc-0054-moscow-evidence-v1",
            expected_prestate=preview["prestate_sha256"],
            expected_candidate=preview["candidate_sha256"],
        )
        assert repeat["disposition"] == "already_applied"
        assert repeat["row_insert_count"] == 0
        assert _protected_image(path) == before

        complete = registry.collect()["source_generation"]
        assert complete["complete"] is True
        assert complete["cardinality"] == {
            "warehouse_count": 2,
            "requested_chrt_count": 2,
            "expected_dense_row_count": 4,
            "actual_dense_row_count": 4,
            "explicit_wb_row_count": 2,
            "explicit_zero_count": 0,
            "omitted_requested_zero_count": 2,
        }
        assert {
            (row["seller_warehouse_id"], row["official_office_id"])
            for row in complete["warehouses"]
        } == {
            (MOSCOW_WAREHOUSE_ID, MOSCOW_OFFICE_ID),
            (ORENBURG_WAREHOUSE_ID, ORENBURG_OFFICE_ID),
        }

        restore_request = {
            **request,
            "mode": RESTORE_MODE,
            "restore_from_version_id": submitted["version_id"],
        }
        collision_preview = service.preview(
            restore_request, "wbc-0054-moscow-evidence-v1"
        )
        try:
            service.apply(
                restore_request,
                "wbc-0054-moscow-evidence-v1",
                expected_prestate=collision_preview["prestate_sha256"],
                expected_candidate=collision_preview["candidate_sha256"],
            )
        except WbFbsMappingEvidenceError as exc:
            assert exc.code == "operation_id_identity_conflict"
        else:
            raise AssertionError("operation ID collision was accepted")
        restore_preview = service.preview(
            restore_request, "wbc-0054-moscow-evidence-restore-v1"
        )
        assert restore_preview["scope"]["official_office_id"] == 0
        restored = service.apply(
            restore_request,
            "wbc-0054-moscow-evidence-restore-v1",
            expected_prestate=restore_preview["prestate_sha256"],
            expected_candidate=restore_preview["candidate_sha256"],
        )
        assert restored["disposition"] == "submitted"
        assert service.readback(
            restore_request, "wbc-0054-moscow-evidence-restore-v1"
        )["state"] == "applied"
        assert registry.collect()["source_generation"]["generation_id"] == complete[
            "generation_id"
        ]
        assert _protected_image(path) == before

        _assert_immutable_versions(path)
        _assert_source_drift_blocks(path, clock)
    print("wb fbs mapping evidence smoke: ok")
    return 0


def _seed(path: Path) -> None:
    now = "2026-09-05T11:00:00Z"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_ff_pool_foundation_schema(conn)
        ensure_wb_fbs_orders_schema(conn)
        ensure_wb_fbs_warehouse_registry_schema(conn)
        conn.execute(
            """CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                   item_id TEXT PRIMARY KEY,nm_id INTEGER,is_active INTEGER,
                   is_hidden INTEGER,updated_at TEXT)"""
        )
        conn.executemany(
            "INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?)",
            [("sku-101", 101, 1, 0, now), ("sku-202", 202, 1, 0, now)],
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                   epoch,writer_enabled,reader_enabled,source_revision,created_at,
                   metadata_json
               ) VALUES(1,1,1,'fixture-epoch',?,'{{}}')""",
            (now,),
        )
        for facility_id, code, name, city in (
            (MOSCOW_FACILITY_ID, "A", "Facility Moscow", "Moscow"),
            (ORENBURG_FACILITY_ID, "B", "Facility Orenburg", "Orenburg"),
        ):
            conn.execute(
                f"""INSERT INTO {FACILITIES_TABLE}(
                       facility_id,code,name,active,display_timezone,created_at,updated_at
                   ) VALUES(?,?,?,1,'UTC',?,?)""",
                (facility_id, code, name, now, now),
            )
            conn.execute(
                f"""INSERT INTO {FACILITY_PROFILES_TABLE}(
                       facility_id,city,future_fields_json,created_at,updated_at
                   ) VALUES(?,?,'{{}}',?,?)""",
                (facility_id, city, now, now),
            )
        conn.executemany(
            f"""INSERT INTO {BALANCES_TABLE}(
                   facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                   wac_rub,source_watermark,updated_at
               ) VALUES(?,?,?,1,?,?,?,? ,?)""",
            [
                (MOSCOW_FACILITY_ID, "FBS", 101, 7, "700", "100", "fixture", now),
                (ORENBURG_FACILITY_ID, "FBS", 101, 4, "400", "100", "fixture", now),
            ],
        )
        conn.execute(
            f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
                   mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                   created_at,created_by,official_office_id,official_warehouse_name,
                   official_office_name,official_office_city,official_evidence_digest
               ) VALUES(?,?,?,?,1,?,?,0,'','','','')""",
            (
                MOSCOW_MAPPING_ID,
                MOSCOW_WAREHOUSE_ID,
                MOSCOW_FACILITY_ID,
                MOSCOW_MAPPING_DIGEST,
                now,
                "fixture-legacy",
            ),
        )
        conn.execute(
            f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
                   mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                   created_at,created_by,official_office_id,official_warehouse_name,
                   official_office_name,official_office_city,official_evidence_digest
               ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?)""",
            (
                "fbs_wh_orenburg_fixture",
                ORENBURG_WAREHOUSE_ID,
                ORENBURG_FACILITY_ID,
                "sha256:" + "f" * 64,
                now,
                "fixture",
                ORENBURG_OFFICE_ID,
                "Official Orenburg warehouse",
                "Official Orenburg office",
                "Orenburg",
                "sha256:" + "b" * 64,
            ),
        )
        conn.commit()


def _protected_image(path: Path):
    with sqlite3.connect(path) as conn:
        mapping = conn.execute(
            f"""SELECT mapping_id,seller_warehouse_id,facility_id,mapping_digest,
                       active,created_at,created_by,official_office_id,
                       official_warehouse_name,official_office_name,
                       official_office_city,official_evidence_digest
                  FROM {WAREHOUSE_MAPPINGS_TABLE} ORDER BY mapping_id"""
        ).fetchall()
        balances = conn.execute(
            f"""SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                  FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id"""
        ).fetchall()
        return mapping, balances


def _assert_immutable_versions(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        version_id = conn.execute(
            f"SELECT version_id FROM {MAPPING_EVIDENCE_VERSIONS_TABLE} "
            "ORDER BY version_sequence LIMIT 1"
        ).fetchone()[0]
        try:
            conn.execute(
                f"UPDATE {MAPPING_EVIDENCE_VERSIONS_TABLE} SET actor='changed' "
                "WHERE version_id=?",
                (version_id,),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("mapping evidence version update was accepted")
        try:
            conn.execute(
                f"DELETE FROM {MAPPING_EVIDENCE_VERSIONS_TABLE} WHERE version_id=?",
                (version_id,),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("mapping evidence version delete was accepted")


def _assert_source_drift_blocks(path: Path, clock: Clock) -> None:
    service = WbFbsMappingEvidenceUpgrade(
        db_path=path,
        storage_identity={
            "generation_id": "operational-fixture",
            "generation_epoch": "fixture",
            "manifest_sha256": "sha256:" + "a" * 64,
        },
        source=OfficeDriftSource(),
        timestamp_factory=clock,
        actor="fixture",
    )
    request = {
        "mode": UPGRADE_MODE,
        "mapping_id": MOSCOW_MAPPING_ID,
        "seller_warehouse_id": MOSCOW_WAREHOUSE_ID,
        "facility_id": MOSCOW_FACILITY_ID,
        "expected_mapping_digest": MOSCOW_MAPPING_DIGEST,
        "expected_office_id": MOSCOW_OFFICE_ID,
    }
    try:
        service.preview(request, "wbc-0054-source-drift-proof")
    except WbFbsMappingEvidenceError as exc:
        assert exc.code in {"official_registry_unstable", "official_target_identity_invalid"}
    else:
        raise AssertionError("official source drift was accepted")


if __name__ == "__main__":
    raise SystemExit(main())
