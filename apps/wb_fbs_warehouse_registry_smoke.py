#!/usr/bin/env python3
"""Synthetic registry, stock reconciliation and exact binding proof."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_fbs_orders import (  # noqa: E402
    WbFbsOffice,
    WbFbsSellerWarehouse,
    WbFbsStock,
)
from packages.adapters.wb_content import (  # noqa: E402
    WbContentCard,
    WbContentCatalogSnapshot,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    ensure_ff_pool_foundation_schema,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    OBSERVATIONS_TABLE,
    ensure_wb_fbs_orders_schema,
)
from packages.application.wb_fbs_warehouse_registry import (  # noqa: E402
    WbFbsWarehouseRegistry,
    WbFbsWarehouseRegistryError,
)


class FakeSource:
    def list_seller_warehouses(self):
        return [
            WbFbsSellerWarehouse(warehouse_id=7001, office_id=501, name="Official A", cargo_type=1, delivery_type=1, is_deleting=False, is_processing=False),
            WbFbsSellerWarehouse(warehouse_id=7002, office_id=502, name="Official B", cargo_type=1, delivery_type=1, is_deleting=False, is_processing=False),
        ]

    def list_offices(self):
        return [
            WbFbsOffice(office_id=501, name="Office A", city="City A", federal_district="District A"),
            WbFbsOffice(office_id=502, name="Office B", city="City B", federal_district="District B"),
        ]

    def list_stocks(self, *, warehouse_id, chrt_ids):
        return [
            WbFbsStock(chrt_id=chrt_id, amount=(9 if warehouse_id == 7001 else 4))
            for chrt_id in chrt_ids
        ]


class StockFailureSource(FakeSource):
    def list_stocks(self, *, warehouse_id, chrt_ids):
        raise RuntimeError("synthetic official stock read failure")


class CatalogSource:
    def fetch_catalog_snapshot(self):
        return WbContentCatalogSnapshot.from_cards(
            [
                WbContentCard(
                    nm_id=101,
                    vendor_code="fixture-101",
                    title="Fixture 101",
                    subject_name="Fixture",
                    updated_at="2026-08-24T00:00:00Z",
                    barcodes=["barcode-101"],
                    chrt_ids=[9001],
                )
            ],
            pages_fetched=1,
            terminal_short_page=True,
            cursor_chain_digest="sha256:fixture-cursor",
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wb-fbs-registry-") as raw:
        db_path = Path(raw) / "operational.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_ff_pool_foundation_schema(conn)
            ensure_wb_fbs_orders_schema(conn)
            conn.execute(
                """CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                       item_id TEXT PRIMARY KEY,nm_id INTEGER,is_active INTEGER,
                       is_hidden INTEGER,updated_at TEXT)"""
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_nomenclature_items
                   VALUES('fixture-101',101,1,0,'2026-08-24T00:00:00Z')"""
            )
            conn.execute(
                f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,1,1,'fixture','2026-08-24T00:00:00Z','{{}}')"
            )
            for facility_id, code, active in (("ff_a", "A", 1), ("ff_wait", "WAIT", 0)):
                conn.execute(
                    f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,'UTC','2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')",
                    (facility_id, code, f"Facility {code}", active),
                )
                conn.execute(
                    f"INSERT INTO {FACILITY_PROFILES_TABLE} VALUES(?,'','{{}}','2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')",
                    (facility_id,),
                )
            conn.execute(
                f"INSERT INTO {BALANCES_TABLE} VALUES('ff_a','FBS',101,1,7,'700','100','fixture','2026-08-24T00:00:00Z')"
            )
            conn.execute(
                f"""INSERT INTO {OBSERVATIONS_TABLE}(
                       observation_id,order_id,source_revision,supply_id,delivery_type,
                       source_created_at,warehouse_id,office_id,nm_id,chrt_id,
                       seller_sku,rid_sha256,order_uid_sha256,skus_json,cargo_type,
                       cross_border_type,is_zero_order,observed_at,collector_date_from,
                       collector_date_to,collector_cursor
                   ) VALUES('obs-0001',1,'revision-0001','','fbs','',7001,501,101,9001,
                            '','','','[]',NULL,NULL,0,'2026-08-24T00:00:00Z',1,1,0)"""
            )
            conn.commit()
        moments = iter(
            [
                "2026-08-24T01:00:00Z", "2026-08-24T01:00:01Z",
                "2026-08-24T01:00:02Z", "2026-08-24T01:00:03Z",
                "2026-08-24T01:00:04Z", "2026-08-24T01:00:05Z",
                "2026-08-24T01:00:06Z", "2026-08-24T01:00:07Z",
                "2026-08-24T01:00:08Z", "2026-08-24T01:00:09Z",
                "2026-08-24T01:00:10Z", "2026-08-24T01:00:11Z",
            ]
        )
        registry = WbFbsWarehouseRegistry(
            db_path=db_path,
            timestamp_factory=lambda: next(moments),
            source=FakeSource(),
            catalog_source=CatalogSource(),
            writer_enabled=True,
        )
        payload = registry.collect()
        assert payload["status"] == "ready" and len(payload["warehouses"]) == 2
        official_a = next(row for row in payload["warehouses"] if row["seller_warehouse_id"] == 7001)
        assert official_a["binding_status"] == "Не привязан"
        stock = official_a["stock_readback"]
        assert stock["complete"] is False and stock["status"] == "unavailable"
        before = _physical_image(db_path)
        preview = registry.preview_binding(
            {"request_id": "binding-fixture-0001", "seller_warehouse_id": 7001, "facility_id": "ff_a"},
            actor="fixture-operator",
        )
        assert preview["preview"]["effect"]["create_inventory_or_movement"] is False
        result = registry.confirm_binding(
            preview["request_id"],
            preview_fingerprint=preview["preview_fingerprint"],
            actor="fixture-operator",
        )
        assert result["seller_warehouse_id"] == 7001
        assert result["bounded_recovery_scope"]["global_backlog_replay"] is False
        assert _physical_image(db_path) == before
        after = registry.collect()
        official_a = next(row for row in after["warehouses"] if row["seller_warehouse_id"] == 7001)
        assert after["source_generation"]["complete"] is True
        assert official_a["stock_readback"]["rows"][0]["internal_physical_quantity"] == 7
        assert official_a["stock_readback"]["rows"][0]["delta_quantity"] == 2
        assert any(row["facility_id"] == "ff_wait" for row in after["waiting_facilities"])
        try:
            registry.preview_binding(
                {"request_id": "binding-fixture-0002", "seller_warehouse_id": 7002, "facility_id": "ff_a"},
                actor="fixture-operator",
            )
        except WbFbsWarehouseRegistryError as exc:
            assert exc.code == "active_binding_conflict"
        else:
            raise AssertionError("one active facility must not bind to two WB warehouses")
        try:
            registry.preview_binding(
                {
                    "request_id": "binding-fixture-0003",
                    "seller_warehouse_id": 7001,
                    "facility_id": "ff_wait",
                },
                actor="fixture-operator",
            )
        except WbFbsWarehouseRegistryError as exc:
            assert exc.code == "active_binding_conflict"
        else:
            raise AssertionError("one WB warehouse must not bind to two active facilities")
        failed_moments = iter(
            [
                "2026-08-24T02:00:00Z",
                "2026-08-24T02:00:01Z",
                "2026-08-24T02:00:02Z",
                "2026-08-24T02:00:03Z",
                "2026-08-24T02:00:04Z",
                "2026-08-24T02:00:05Z",
            ]
        )
        failed_stock_registry = WbFbsWarehouseRegistry(
            db_path=db_path,
            timestamp_factory=lambda: next(failed_moments),
            source=StockFailureSource(),
            catalog_source=CatalogSource(),
        )
        before_failure = _physical_image(db_path)
        failed_readback = failed_stock_registry.collect()
        assert failed_readback["status"] == "ready"
        failed_bound = next(
            item for item in failed_readback["warehouses"]
            if item["seller_warehouse_id"] == 7001
        )
        assert failed_bound["stock_readback"]["status"] == "failed"
        assert failed_bound["stock_readback"]["complete"] is False
        assert all(
            row["wb_declared_quantity"] is None and row["delta_quantity"] is None
            for row in failed_bound["stock_readback"]["rows"]
        )
        assert failed_readback["source_generation"]["complete"] is True
        assert _physical_image(db_path) == before_failure
    print("wb fbs warehouse registry smoke: ok")
    return 0


def _physical_image(path: Path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            f"SELECT facility_id,pool,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} ORDER BY 1,2,3"
        ).fetchall()


if __name__ == "__main__":
    raise SystemExit(main())
