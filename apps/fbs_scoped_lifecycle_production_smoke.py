#!/usr/bin/env python3
"""Real commit/reopen verification on an isolated admitted production fixture."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_scoped_recovery_smoke as fixture
from packages.application import fbs_scoped_lifecycle_production as production
from packages.application import fbs_historical_fbo_scoped_recovery as core
from packages.application.storage_registry import StoreRegistry
from apps.production_apply_launcher import execute
NOW=fixture.NOW


class FrozenClock(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,8,16,12,tzinfo=timezone.utc)


class ProductionRecoveryTests(unittest.TestCase):
    connection=fixture.ScopedRecoveryTests.connection
    snapshot=fixture.ScopedRecoveryTests.snapshot
    def setUp(self):
        fixture.ScopedRecoveryTests.setUp(self)
        root=self.runtime.runtime_dir
        (root.parent/'app').mkdir();(root.parent/'app'/'.wb-core-runtime-sha').write_text('a'*40)
        for name,value in [('PRODUCTION_ROOT',root),('datetime',FrozenClock),
                ('_held_binding',lambda r,q:dict(window_id='held-fixture',proof='unchanged')),
                ('_quiet_activity',lambda r,q:dict(quiet=True))]:
            guard=patch.object(production,name,value);guard.start();self.addCleanup(guard.stop)
        with self.connection() as conn:
            for table in ('sheet_vitrina_v1_fulfillment_service_uploads','sheet_vitrina_v1_fulfillment_service_lines',
                    'wb_finance_weekly_aggregates','wb_finance_weekly_sku_aggregates'):
                conn.execute(f'CREATE TABLE IF NOT EXISTS {table}(source_id TEXT,amount_rub TEXT)')
            for i in range(7):
                conn.execute('INSERT INTO sheet_vitrina_v1_fulfillment_service_uploads VALUES(?,?)',(str(i),'100'))
                conn.execute('INSERT INTO sheet_vitrina_v1_fulfillment_service_lines VALUES(?,?)',(str(i),'100'))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(version_id,cutover_id,version_kind,effective_at,business_effective_date,published_at,status,plan_fingerprint,local_source_digest,source_watermarks_json,created_at) VALUES(?,?,?,?,?,?,'good',?,?,?,?)",
                ('wf_stage7c','warehouse_functional_cutover_v1','hourly_wb_sync',NOW,'2026-08-16',NOW,'source-fingerprint','source-digest','{}',NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,items_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('source-snapshot','wf_stage7c',NOW,'2026-08-16','[101,102]',1,1,'[0]',0,'raw-digest','[]','[]',NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(slot,active_version_id,updated_at) VALUES(1,?,?)",('wf_stage7c',NOW))
            conn.commit()
        with sqlite3.connect(root/'fbs-snapshot-accounting.sqlite3') as conn:
            conn.execute('CREATE TABLE accounting_current(singleton INTEGER,version TEXT)')
            conn.execute('CREATE TABLE accounting_revisions(version TEXT,operation_id TEXT,payload TEXT,previous_version TEXT)')
            conn.execute('CREATE TABLE accounting_blobs(digest TEXT,payload BLOB)')
            conn.execute("INSERT INTO accounting_current VALUES(1,'book-with-seven-expenses')")
            conn.execute('INSERT INTO accounting_revisions VALUES(?,?,?,NULL)',('book-with-seven-expenses','seed',json.dumps({'expenses':[{'id':i,'amount':'100'} for i in range(7)]})))
            conn.execute('INSERT INTO accounting_blobs VALUES(?,?)',('seven-expense-source',b'exact original seven expenses'))
        self.request=dict(runtime_dir=str(root),runtime_sha='a'*40,storage_manifest_sha256=StoreRegistry(root).load().manifest_sha256,
            window_id='held-fixture',expected_baseline_fingerprint='fixture',maintenance_env_file='/not-read',
            facility_id='fac_moscow',nm_id=101,expected_order_count=2,expected_status_count=3,expected_handoff_count=1,
            approval_reference='explicit-isolated-fixture',actor='smoke')
        self.adapter=production.FbsScopedLifecycleProductionAdapter();self.operation='scoped-fixture-0001'

    def test_real_commit_durable_readback_and_launcher_retry_preserve_seven_expenses(self):
        preview=self.adapter.preview(self.request,self.operation)
        with self.connection() as conn:before=production.protected_scope(conn,self.runtime.runtime_dir)
        result=execute(action='apply',adapter_name='fixture',operation_id=self.operation,request=self.request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'fixture':self.adapter})
        self.assertEqual(result['state'],'applied')
        self.assertEqual(self.adapter.readback(self.request,self.operation)['state'],'applied')
        again=execute(action='apply',adapter_name='fixture',operation_id=self.operation,request=self.request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'fixture':self.adapter})
        self.assertEqual(again['state'],'applied')
        with self.connection() as conn:
            self.assertEqual(before,production.protected_scope(conn,self.runtime.runtime_dir))
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {core.PRODUCTION_RECEIPTS}').fetchone()[0],1)

    def test_source_cas_changed_rejects_without_claim_or_native_effect(self):
        preview=self.adapter.preview(self.request,self.operation)
        with self.connection() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ff_pool_balances SET source_watermark='drift' WHERE pool='FBS' AND nm_id=101");conn.commit()
        before=self.snapshot()
        with self.assertRaisesRegex(Exception,'preview_compare_and_swap_failed'):
            self.adapter.apply(self.request,self.operation,preview)
        self.assertEqual(before,self.snapshot())
        self.assertFalse(production._paths(self.runtime.runtime_dir,self.operation)[1].exists())

    def test_wrong_current_date_and_expanded_scope_reject_readonly(self):
        changed={**self.request,'expected_order_count':3}
        with self.assertRaisesRegex(Exception,'reviewed_scope_changed'):self.adapter.preview(changed,self.operation)
        with self.connection() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET business_effective_date='2026-08-15'");conn.commit()
        before=self.snapshot()
        with self.assertRaisesRegex(Exception,'current_business_date_required'):self.adapter.preview(self.request,self.operation)
        self.assertEqual(before,self.snapshot())

    def test_unadmitted_production_core_cannot_bypass_private_wrapper(self):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            recipe=core.prepare_scoped_fbs_recovery(conn,operation_id=self.operation,facility_id='fac_moscow',nm_id=101)
            with self.assertRaisesRegex(Exception,'held_owner_required'):
                core.apply_scoped_fbs_recovery_owned(conn,recipe=recipe,occurred_at=NOW,actor='smoke',receipt_table=core.PRODUCTION_RECEIPTS)
            conn.rollback()

    def test_existing_aggregate_gap_is_not_silently_absorbed_by_first_handoff(self):
        with self.connection() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quantity='999' WHERE warehouse_key='ff' AND nm_id=101");conn.commit()
        before=self.snapshot()
        with self.assertRaisesRegex(Exception,'initial_pool_aggregate_parity_unproved'):
            self.adapter.preview(self.request,self.operation)
        self.assertEqual(before,self.snapshot())

    def test_committed_native_truth_survives_retention_failure_and_later_accounting(self):
        preview=self.adapter.preview(self.request,self.operation)
        with patch.object(production.WarehouseRecoveryRegistry,'retain',side_effect=RuntimeError('retention interrupted')):
            receipt=execute(action='apply',adapter_name='fixture',operation_id=self.operation,request=self.request,
                expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'fixture':self.adapter})
        self.assertEqual(receipt['state'],'applied')
        self.assertTrue(receipt['readback']['recovery_pending'])
        with sqlite3.connect(self.runtime.runtime_dir/'fbs-snapshot-accounting.sqlite3') as conn:
            conn.execute("UPDATE accounting_current SET version='legitimate-later-book'");conn.commit()
        with self.connection() as conn:
            fixture._insert_post_t_order(conn,order_id=9799,supplier='new',wb='waiting',
                source_created_at='2026-08-16T11:00:00Z',observed_at='2026-08-16T11:01:00Z',
                source_chrt_id=999,identity_outcome='unmatched_identity');conn.commit()
        later=self.adapter.readback(self.request,self.operation)
        self.assertEqual(later['state'],'applied')
        self.assertFalse(later['protected_scope_unchanged_now'])
        self.assertEqual(later['current_dependency_status'],'blocked')
        self.assertEqual(self.adapter.apply(self.request,self.operation,preview)['disposition'],'already_applied')


if __name__=='__main__':unittest.main()
