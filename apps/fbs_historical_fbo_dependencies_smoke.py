#!/usr/bin/env python3
"""Dependency evidence remains fail closed on disposable SQLite fixtures."""
from pathlib import Path
import json
import sqlite3
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import fbs_historical_fbo_dependencies as dep
from packages.application import ff_pool_fbs_lifecycle as life, wb_fbs_orders as orders


class DependencyPreparationTests(unittest.TestCase):
    def connection(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def fixture(self):
        conn = self.connection()
        definitions = {
            orders.OBSERVATIONS_TABLE: "order_id,observation_sequence,warehouse_id,nm_id,observed_at,source_revision",
            orders.STATUS_CURRENT_TABLE: "order_id,supplier_status,wb_status",
            orders.STATUS_OBSERVATIONS_TABLE: "order_id,observation_sequence,observed_at,supplier_status,wb_status",
            orders.WAREHOUSE_MAPPINGS_TABLE: "seller_warehouse_id,facility_id,active",
            orders.IDENTITY_MAPPINGS_TABLE: "mapping_id",
            orders.IDENTITY_EVIDENCE_TABLE: "order_id,evidence_sequence,outcome",
            life.CURRENT_TABLE: "order_id,facility_id,pool,state",
            life.IDENTITY_PENDING_TABLE: "pending_id,order_id,cutover_id,source_status_observation_sequence",
            life.IDENTITY_PENDING_RESOLUTIONS_TABLE: "pending_id",
            "sheet_vitrina_v1_ff_pool_cutover_manifests": "manifest_json,cutover_at,cutover_id",
        }
        for table, columns in definitions.items():
            conn.execute(f"CREATE TABLE {table}({columns})")
        manifest = dict(cutover_id="cut", cutover_at="2026-08-14T16:35:09Z",
            accounting_boundary=dict(local_boundary_at="2026-08-14T16:11:02Z", status_observation_watermark_sequence=5))
        conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES(?,?,?)",
            (json.dumps(manifest), manifest["cutover_at"], "cut"))
        conn.execute(f"INSERT INTO {orders.WAREHOUSE_MAPPINGS_TABLE} VALUES(1,'moscow',1)")
        conn.commit()
        return conn

    def test_missing_immutable_sources_never_mean_zero(self):
        conn = self.connection(); conn.execute("BEGIN"); conn.execute("PRAGMA query_only=ON")
        result = dep.prepare_fbs_dependencies(conn, facility_id="moscow", nm_id=7)
        self.assertEqual(result["status"], "blocked")
        self.assertIn(orders.STATUS_OBSERVATIONS_TABLE, result["missing_tables"])
        self.assertNotIn("order_count", result)
        self.assertEqual(conn.total_changes, 0)

    def test_post_t_order_with_past_handoff_remains_out_of_scope(self):
        conn = self.fixture()
        conn.execute(f"INSERT INTO {orders.OBSERVATIONS_TABLE} VALUES(10,10,1,7,'2026-08-17T12:00:00Z','rev')")
        conn.execute(f"INSERT INTO {orders.STATUS_CURRENT_TABLE} VALUES(10,'complete','ready_for_pickup')")
        conn.executemany(f"INSERT INTO {orders.STATUS_OBSERVATIONS_TABLE} VALUES(?,?,?,?,?)", [
            (10,10,"2026-08-17T12:00:00Z","complete","sorted"),
            (10,11,"2026-08-18T12:00:00Z","complete","ready_for_pickup")])
        conn.execute(f"INSERT INTO {orders.IDENTITY_EVIDENCE_TABLE} VALUES(10,10,'unmatched_identity')")
        conn.commit(); changes = conn.total_changes
        conn.execute("BEGIN"); conn.execute("PRAGMA query_only=ON")
        result = dep.prepare_fbs_dependencies(conn, facility_id="moscow", nm_id=7)
        self.assertEqual((result["order_count"], result["status_count"], result["orders_with_physical_handoff"]), (1,2,1))
        self.assertIn("exact_matched_identity_missing", result["blockers"])
        self.assertIn("post_cutover_lifecycle_outside_native_transfer_scope", result["blockers"])
        self.assertFalse(result["source"]["orders"][0]["status_before_frozen_watermark"])
        self.assertEqual(conn.total_changes, changes)
        self.assertEqual(result, dep.prepare_fbs_dependencies(conn, facility_id="moscow", nm_id=7))

    def test_empty_exact_cohort_ready_only_with_required_sources(self):
        conn = self.fixture(); conn.execute("BEGIN"); conn.execute("PRAGMA query_only=ON")
        result = dep.prepare_fbs_dependencies(conn, facility_id="moscow", nm_id=7)
        self.assertEqual((result["status"], result["order_count"]), ("ready", 0))


if __name__ == "__main__": unittest.main()
