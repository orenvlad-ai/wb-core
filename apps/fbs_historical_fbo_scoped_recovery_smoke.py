#!/usr/bin/env python3
"""Exact identity, bounded lifecycle, rollback and later replay fixtures."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.ff_pool_fbs_forward_recovery_smoke import _prepared_runtime
from apps.ff_pool_fbs_lifecycle_smoke import _insert_post_t_order, _append_status
from packages.application import fbs_historical_fbo_scoped_recovery as recovery
from packages.application import ff_pool_fbs_lifecycle as life, wb_fbs_orders as orders

NOW = "2026-08-16T12:00:00Z"


class ScopedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "private-evidence"
        self.runtime = _prepared_runtime(self.root)
        with self.connection() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET vendor_code='seller-101' WHERE nm_id=101")
            for oid, supplier, wb in [(9700,"complete","sorted"), (9701,"new","waiting")]:
                _insert_post_t_order(conn, order_id=oid, supplier=supplier, wb=wb,
                    observed_at="2026-08-16T09:00:00Z", source_created_at="2026-08-16T08:00:00Z",
                    source_chrt_id=999, identity_outcome="unmatched_identity")
            _append_status(conn, order_id=9700, revision="new-revision", supplier_status="complete",
                wb_status="ready_for_pickup", episode=2, observed_at="2026-08-16T10:00:00Z", insert_current=True)
            conn.commit()

    def connection(self):
        conn = sqlite3.connect(self.runtime.db_path); conn.row_factory = sqlite3.Row
        return closing(conn)

    def plan(self, conn):
        return recovery.prepare_scoped_fbs_recovery(conn, operation_id="fixture-scoped", facility_id="fac_moscow", nm_id=101)

    def snapshot(self):
        with self.connection() as conn:
            return {r[0]: [tuple(v) for v in conn.execute('SELECT * FROM "' + r[0] + '" ORDER BY rowid')]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def test_exact_owned_handoff_and_later_global_replay_do_not_debit_twice(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE"); recipe = self.plan(conn)
            self.assertEqual(len(recipe["predicted_handoffs"]), 1)
            result = recovery.apply_scoped_fbs_recovery(conn, recipe=recipe,
                runtime_dir=self.runtime.runtime_dir, occurred_at=NOW, actor="test")
            self.assertEqual(result["physical_handoffs"], recipe["predicted_handoffs"])
            self.assertEqual(result["lifecycle"]["summary"]["fulfilled"], 1)
            conn.commit()
            state = recovery._cursor_state(conn)
            conn.execute("BEGIN IMMEDIATE")
            again = recovery.apply_scoped_fbs_recovery(conn, recipe=recipe,
                runtime_dir=self.runtime.runtime_dir, occurred_at=NOW, actor="test")
            self.assertTrue(again["idempotent"]); conn.commit()
            self.assertEqual(state, recovery._cursor_state(conn))
            before = tuple(conn.execute(f"SELECT quantity,capital_rub FROM {life.BALANCES_TABLE} WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101").fetchone())
            conn.execute("BEGIN IMMEDIATE")
            life.process_post_t_fbs_lifecycle(conn, occurred_at=NOW, limit=100, schema_ready=True)
            after = tuple(conn.execute(f"SELECT quantity,capital_rub FROM {life.BALANCES_TABLE} WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101").fetchone())
            self.assertEqual(before, after); conn.rollback()

    def test_all_identity_and_native_effects_rollback(self):
        before = self.snapshot()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE"); recipe = self.plan(conn)
            recovery.apply_scoped_fbs_recovery(conn, recipe=recipe,
                runtime_dir=self.runtime.runtime_dir, occurred_at=NOW, actor="test")
            conn.rollback()
        self.assertEqual(before, self.snapshot())

    def test_later_cancellation_uses_reconciliation_and_never_returns_physical_stock(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE"); recipe = self.plan(conn)
            result = recovery.apply_scoped_fbs_recovery(conn, recipe=recipe,
                runtime_dir=self.runtime.runtime_dir, occurred_at=NOW, actor="test")
            conn.commit()
            _append_status(conn, order_id=9700, revision="cancel-revision", supplier_status="cancel",
                wb_status="canceled", episode=3, observed_at="2026-08-16T11:00:00Z", insert_current=True)
            collector=orders.WbFbsOrdersCollector(db_path=self.runtime.db_path,timestamp_factory=lambda:NOW,source=object(),enabled=False)
            collector._persist_identity_evidence(conn,recovery._rows(conn,orders.OBSERVATIONS_TABLE,"order_id=?",(9700,)))
            conn.commit(); conn.execute("BEGIN IMMEDIATE")
            handled = life.process_post_t_fbs_lifecycle(conn, occurred_at=NOW, limit=100, schema_ready=True)
            self.assertEqual(handled["summary"]["fulfilled"], 0)
            self.assertEqual(handled["summary"]["reconciliation"], 1)
            balance = conn.execute(f"SELECT quantity,capital_rub FROM {life.BALANCES_TABLE} WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101").fetchone()
            self.assertEqual(dict(balance), result["final_balance"]); conn.rollback()

    def test_native_material_publication_and_durable_continuation(self):
        with self.connection() as conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(version_id,cutover_id,version_kind,effective_at,business_effective_date,published_at,status,plan_fingerprint,local_source_digest,source_watermarks_json,created_at) VALUES(?,?,?,?,?,?,'good',?,?,?,?)",
                ('wf_stage7c','warehouse_functional_cutover_v1','hourly_wb_sync',NOW,'2026-08-16',NOW,'source-fingerprint','source-digest','{}',NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,items_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('source-snapshot','wf_stage7c',NOW,'2026-08-16','[101,102]',1,1,'[0]',0,'raw-digest','[]','[]',NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(slot,active_version_id,updated_at) VALUES(1,?,?)",('wf_stage7c',NOW))
            conn.commit(); conn.execute('BEGIN IMMEDIATE'); recipe=self.plan(conn)
            result=recovery.apply_scoped_fbs_recovery(conn, recipe=recipe,
                runtime_dir=self.runtime.runtime_dir, occurred_at=NOW, actor='test')
            active=conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1').fetchone()[0]
            self.assertNotEqual(active,'wf_stage7c')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id LIKE ?',('fbs_lifecycle:%',)).fetchone()[0],1)
            self.assertEqual(len(result['native_receipts'][life.OPERATIONS_TABLE]),1)
            conn.rollback()

    def test_source_drift_is_blocked_before_identity_creation(self):
        with self.connection() as conn:
            conn.execute("BEGIN"); recipe = self.plan(conn); conn.rollback()
            conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET vendor_code='changed' WHERE nm_id=101"); conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "canonical_identity_unproved|source_compare_and_swap_failed"):
                recovery.apply_scoped_fbs_recovery(conn, recipe=recipe,
                    runtime_dir=self.runtime.runtime_dir, occurred_at=NOW, actor="test")
            conn.rollback()
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {orders.IDENTITY_MAPPINGS_TABLE} WHERE source_chrt_id=999").fetchone()[0],0)

    def test_wrong_recipe_and_non_private_destination_rejected(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE"); recipe = self.plan(conn)
            with self.assertRaisesRegex(ValueError,"private_copy_required"):
                recovery.apply_scoped_fbs_recovery(conn,recipe=recipe,runtime_dir=self.root/'other',occurred_at=NOW,actor='test')
            recipe["predicted_handoffs"][0]["capital_delta_rub"] = "-1"
            with self.assertRaisesRegex(ValueError,"recipe_digest_invalid"):
                recovery.apply_scoped_fbs_recovery(conn,recipe=recipe,runtime_dir=self.runtime.runtime_dir,occurred_at=NOW,actor='test')
            conn.rollback()

    def test_retry_refuses_tampered_identity_receipt(self):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); recipe=self.plan(conn)
            recovery.apply_scoped_fbs_recovery(conn,recipe=recipe,runtime_dir=self.runtime.runtime_dir,occurred_at=NOW,actor='test');conn.commit()
            conn.execute('DROP TRIGGER wb_fbs_identity_mapping_no_update')
            conn.execute(f"UPDATE {orders.IDENTITY_MAPPINGS_TABLE} SET active=0 WHERE source_chrt_id=999");conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError,'existing_identity_receipt_conflict'):
                recovery.apply_scoped_fbs_recovery(conn,recipe=recipe,runtime_dir=self.runtime.runtime_dir,occurred_at=NOW,actor='test')
            conn.rollback()


if __name__ == "__main__": unittest.main()
