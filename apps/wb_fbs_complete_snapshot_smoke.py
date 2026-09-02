#!/usr/bin/env python3
"""Full exact-catalog FBS source generation and omission provenance proof."""

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
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    ensure_ff_pool_foundation_schema,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    WAREHOUSE_MAPPINGS_TABLE,
    ensure_wb_fbs_orders_schema,
)
from packages.application.wb_fbs_warehouse_registry import (  # noqa: E402
    COMPLETE_CATALOG_OMISSION_ZERO_POLICY,
    STOCK_ROWS_TABLE,
    WbFbsWarehouseRegistry,
)


MOSCOW_WAREHOUSE_ID = 1988668
MOSCOW_OFFICE_ID = 14017
ORENBURG_WAREHOUSE_ID = 854205
ORENBURG_OFFICE_ID = 12223


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        current = self.value
        self.value += timedelta(seconds=1)
        return current.isoformat().replace("+00:00", "Z")


class OfficialSource:
    def list_seller_warehouses(self):
        return [
            WbFbsSellerWarehouse(
                warehouse_id=MOSCOW_WAREHOUSE_ID,
                office_id=MOSCOW_OFFICE_ID,
                name="FBS Москва",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=ORENBURG_WAREHOUSE_ID,
                office_id=ORENBURG_OFFICE_ID,
                name="FBS Оренбург",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
            WbFbsSellerWarehouse(
                warehouse_id=777777,
                office_id=777,
                name="Unbound evidence only",
                cargo_type=1,
                delivery_type=1,
                is_deleting=False,
                is_processing=False,
            ),
        ]

    def list_offices(self):
        return [
            WbFbsOffice(
                office_id=MOSCOW_OFFICE_ID,
                name="Москва (Софьино)",
                city="Москва_Восток",
                federal_district="ЦФО",
            ),
            WbFbsOffice(
                office_id=ORENBURG_OFFICE_ID,
                name="Оренбург Центральная",
                city="Оренбург",
                federal_district="ПФО",
            ),
            WbFbsOffice(
                office_id=777,
                name="Unbound office",
                city="Казань",
                federal_district="ПФО",
            ),
        ]

    def list_stocks(self, *, warehouse_id, chrt_ids):
        assert chrt_ids == [9001, 9002, 9003], chrt_ids
        if warehouse_id == MOSCOW_WAREHOUSE_ID:
            return [
                WbFbsStock(chrt_id=9001, amount=0),
                WbFbsStock(chrt_id=9003, amount=5),
            ]
        if warehouse_id == ORENBURG_WAREHOUSE_ID:
            return [
                WbFbsStock(chrt_id=9001, amount=2),
                WbFbsStock(chrt_id=9002, amount=0),
            ]
        raise AssertionError("unbound seller warehouse must not enter complete source scope")


class StockFailureSource(OfficialSource):
    def list_stocks(self, *, warehouse_id, chrt_ids):
        if warehouse_id == ORENBURG_WAREHOUSE_ID:
            raise RuntimeError("synthetic Orenburg stock read failure")
        return super().list_stocks(warehouse_id=warehouse_id, chrt_ids=chrt_ids)


class OfficeMismatchSource(OfficialSource):
    def list_seller_warehouses(self):
        rows = super().list_seller_warehouses()
        return [
            WbFbsSellerWarehouse(
                warehouse_id=row.warehouse_id,
                office_id=14018 if row.warehouse_id == MOSCOW_WAREHOUSE_ID else row.office_id,
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
                office_id=14018,
                name="Moscow wrong live office",
                city="Москва_Восток",
                federal_district="ЦФО",
            )
        ]


class CatalogSource:
    def __init__(self, *, drift: bool = False) -> None:
        self.calls = 0
        self.drift = drift

    def fetch_catalog_snapshot(self):
        self.calls += 1
        cards = [
            WbContentCard(
                nm_id=101,
                vendor_code="ACTIVE-101",
                title="Active 101",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:00Z",
                barcodes=["bc-101-a", "bc-101-b"],
                chrt_ids=[9001, 9002],
            ),
            WbContentCard(
                nm_id=202,
                vendor_code="ACTIVE-202",
                title="Active 202",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:01Z",
                barcodes=["bc-202"],
                chrt_ids=[9003] + ([9999] if self.drift and self.calls % 2 == 0 else []),
            ),
            WbContentCard(
                nm_id=303,
                vendor_code="HIDDEN-303",
                title="Hidden 303",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:02Z",
                barcodes=["bc-303"],
                chrt_ids=[9303],
            ),
            WbContentCard(
                nm_id=404,
                vendor_code="INACTIVE-404",
                title="Inactive 404",
                subject_name="Fixture",
                updated_at="2026-09-02T03:00:03Z",
                barcodes=["bc-404"],
                chrt_ids=[9404],
            ),
        ]
        return WbContentCatalogSnapshot.from_cards(
            cards,
            pages_fetched=1,
            terminal_short_page=True,
            cursor_chain_digest="sha256:fixture-cursor-chain",
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wb-fbs-complete-snapshot-") as raw:
        db_path = Path(raw) / "operational.sqlite3"
        _seed(db_path)
        clock = Clock()
        registry = WbFbsWarehouseRegistry(
            db_path=db_path,
            timestamp_factory=clock,
            source=OfficialSource(),
            catalog_source=CatalogSource(),
        )
        good = registry.collect()
        generation = good["source_generation"]
        assert generation["status"] == "complete" and generation["complete"] is True
        assert generation["policy_version"] == COMPLETE_CATALOG_OMISSION_ZERO_POLICY
        assert generation["catalog_scope"]["active_nm_id_count"] == 2
        assert generation["catalog_scope"]["requested_chrt_count"] == 3
        assert generation["warehouse_scope"]["warehouse_count"] == 2
        assert generation["cardinality"] == {
            "warehouse_count": 2,
            "requested_chrt_count": 3,
            "expected_dense_row_count": 6,
            "actual_dense_row_count": 6,
            "explicit_wb_row_count": 4,
            "explicit_zero_count": 2,
            "omitted_requested_zero_count": 2,
        }
        warehouse_pairs = {
            (row["seller_warehouse_id"], row["official_office_id"])
            for row in generation["warehouses"]
        }
        assert warehouse_pairs == {
            (MOSCOW_WAREHOUSE_ID, MOSCOW_OFFICE_ID),
            (ORENBURG_WAREHOUSE_ID, ORENBURG_OFFICE_ID),
        }
        with sqlite3.connect(db_path) as conn:
            provenance = conn.execute(
                f"""SELECT seller_warehouse_id,chrt_id,amount,provenance
                       FROM {STOCK_ROWS_TABLE}
                      WHERE run_id IN (
                            SELECT run_id FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_runs
                             WHERE registry_run_id=?
                      ) ORDER BY seller_warehouse_id,chrt_id""",
                (generation["generation_id"],),
            ).fetchall()
        assert len(provenance) == 6
        assert (MOSCOW_WAREHOUSE_ID, 9001, 0, "explicit_wb_row") in provenance
        assert (MOSCOW_WAREHOUSE_ID, 9002, 0, "omitted_requested_zero") in provenance
        assert (ORENBURG_WAREHOUSE_ID, 9002, 0, "explicit_wb_row") in provenance
        assert (ORENBURG_WAREHOUSE_ID, 9003, 0, "omitted_requested_zero") in provenance

        stable_generation_id = generation["generation_id"]
        drifted = WbFbsWarehouseRegistry(
            db_path=db_path,
            timestamp_factory=clock,
            source=OfficialSource(),
            catalog_source=CatalogSource(drift=True),
        ).collect()
        assert drifted["latest_attempt"]["complete"] == 0
        assert drifted["source_generation"]["generation_id"] == stable_generation_id

        failed = WbFbsWarehouseRegistry(
            db_path=db_path,
            timestamp_factory=clock,
            source=StockFailureSource(),
            catalog_source=CatalogSource(),
        ).collect()
        assert failed["latest_attempt"]["complete"] == 0
        assert failed["source_generation"]["generation_id"] == stable_generation_id

        office_mismatch = WbFbsWarehouseRegistry(
            db_path=db_path,
            timestamp_factory=clock,
            source=OfficeMismatchSource(),
            catalog_source=CatalogSource(),
        ).collect()
        assert office_mismatch["latest_attempt"]["complete"] == 0
        assert office_mismatch["source_generation"]["generation_id"] == stable_generation_id
    print("wb fbs complete snapshot smoke: ok")
    return 0


def _seed(path: Path) -> None:
    now = "2026-09-02T03:00:00Z"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_ff_pool_foundation_schema(conn)
        ensure_wb_fbs_orders_schema(conn)
        conn.execute(
            """CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                   item_id TEXT PRIMARY KEY,nm_id INTEGER,is_active INTEGER,
                   is_hidden INTEGER,updated_at TEXT)"""
        )
        conn.executemany(
            "INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?)",
            [
                ("active-101", 101, 1, 0, now),
                ("active-202", 202, 1, 0, now),
                ("hidden-303", 303, 1, 1, now),
                ("inactive-404", 404, 0, 0, now),
            ],
        )
        for facility_id, code, name, city in (
            ("fac_moscow", "MSK", "FF Москва", "Москва"),
            ("fac_orenburg", "ORE", "FF Оренбург", "Оренбург"),
        ):
            conn.execute(
                f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,1,'Asia/Yekaterinburg',?,?)",
                (facility_id, code, name, now, now),
            )
            conn.execute(
                f"INSERT INTO {FACILITY_PROFILES_TABLE} VALUES(?,?,'{{}}',?,?)",
                (facility_id, city, now, now),
            )
        conn.executemany(
            f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
                   mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                   created_at,created_by,official_office_id,official_warehouse_name,
                   official_office_name,official_office_city,official_evidence_digest
               ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?)""",
            [
                (
                    "map-moscow", MOSCOW_WAREHOUSE_ID, "fac_moscow", "sha256:map-moscow",
                    now, "fixture", MOSCOW_OFFICE_ID, "FBS Москва", "Москва (Софьино)",
                    "Москва_Восток", "sha256:official-moscow",
                ),
                (
                    "map-orenburg", ORENBURG_WAREHOUSE_ID, "fac_orenburg", "sha256:map-orenburg",
                    now, "fixture", ORENBURG_OFFICE_ID, "FBS Оренбург", "Оренбург Центральная",
                    "Оренбург", "sha256:official-orenburg",
                ),
            ],
        )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
