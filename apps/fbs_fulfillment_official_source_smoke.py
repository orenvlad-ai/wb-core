#!/usr/bin/env python3
"""Official-only planner source: admission, freshness and no lifecycle/cost reads."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_official_fbs_smoke import fixture
from packages.application.official_fbs_stock_read import current_official_fbs_facilities, read_complete_official_fbs_stock
from packages.application.wb_fbs_warehouse_registry import FACILITIES_TABLE, WAREHOUSE_MAPPINGS_TABLE
from packages.application.ff_pool_foundation import FACILITY_PROFILES_TABLE


def main():
    now = datetime(2026, 9, 5, 10, 10, tzinfo=timezone.utc)
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "stock.sqlite3"
        conn = fixture(path)
        conn.row_factory = sqlite3.Row
        for column in ['code', 'name']:
            conn.execute(f"ALTER TABLE {FACILITIES_TABLE} ADD COLUMN {column} TEXT")
        conn.execute(f"UPDATE {FACILITIES_TABLE} SET name='FF Москва',code=facility_id")
        conn.execute(f"CREATE TABLE {FACILITY_PROFILES_TABLE}(facility_id,city)")
        conn.commit()
        def read(at=now, ids=(1, 2)):
            return current_official_fbs_facilities(path, requested_nm_ids=ids, now=at)["facilities"]
        before = path.read_bytes()
        assert read()[0]['available'] == 3  # explicit plus dense omitted zero
        assert read()[0]['stock_source']['stock_run_id'] == 'A'
        assert path.read_bytes() == before
        # A newer partial attempt never replaces the last complete generation.
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SELECT 'partial',2,'partial',0,policy_version,catalog_scope_json,warehouse_scope_json,generation_digest,started_at,completed_at FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs WHERE run_id='g1'")
        conn.commit()
        assert read()[0]['available'] == 3
        assert read(now + timedelta(minutes=30))[0]['available'] is None
        assert 'устарел' in read(now + timedelta(days=1))[0]['source_blocker']
        assert read(ids=(1, 2, 3))[0]['available'] is None
        # Quantity reader succeeds while SQLite explicitly denies every costing/lifecycle table.
        def authorize(action, table, *_):
            if action == sqlite3.SQLITE_READ and any(x in (table or '') for x in ['lifecycle','functional','pool_balance','business_projection']):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        assert read_complete_official_fbs_stock(conn, universe=[1, 2], day='2026-09-05', now=now)['available']
        conn.set_authorizer(None)
        conn.execute(f"UPDATE {WAREHOUSE_MAPPINGS_TABLE} SET active=0 WHERE facility_id='A'")
        conn.commit()
        assert 'привязка' in read()[0]['source_blocker']
        conn.execute(f"UPDATE {WAREHOUSE_MAPPINGS_TABLE} SET active=1")
        conn.execute("DELETE FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows WHERE run_id='A' AND nm_id=2")
        conn.commit()
        assert read()[0]['available'] is None
        conn.close()
    print('fbs_fulfillment_official_source_smoke: ok')

if __name__ == '__main__':
    main()
