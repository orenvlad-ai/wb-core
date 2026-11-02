#!/usr/bin/env python3
"""Bounded source selection: native Ready policy, immutable history, source CAS."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_publication as publication
from packages.application.fbs_accounting_rehearsal_sources import select_rehearsal_sources
from packages.application.sheet_vitrina_v1_inventory_history import CAPTURES_TABLE,COMPONENTS_TABLE,FINALIZATIONS_TABLE
from packages.application.fbs_snapshot_cost import canonical

NOW=datetime.fromisoformat('2026-11-02T15:00:00+00:00')

class RehearsalSourcesTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT)')
        self.conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'b')")
        self.conn.execute(f'CREATE TABLE {ready.TABLE}(bundle_version TEXT,as_of_date TEXT,plan_json TEXT,activated_at TEXT,refreshed_at TEXT,PRIMARY KEY(bundle_version,as_of_date))')
        self.conn.execute(f'CREATE TABLE {CAPTURES_TABLE}(capture_sequence INTEGER PRIMARY KEY,capture_id TEXT,business_date TEXT)')
        self.conn.execute(f'CREATE TABLE {COMPONENTS_TABLE}(capture_id TEXT,payload TEXT)')
        self.conn.execute(f'CREATE TABLE {FINALIZATIONS_TABLE}(capture_id TEXT)')
        self.put('2026-11-01',dates=['2026-10-31','2026-11-01','2026-11-02'])
        self.put('2026-10-31')
    def put(self,day,*,dates=None,padding='',metadata=None,bundle='b'):
        dates=dates or [day]
        payload=dict(snapshot_id='snapshot:'+day,date_columns=dates,sheets=[dict(sheet_name='DATA_VITRINA',header=['label','key',*dates])],metadata=metadata or {},padding=padding)
        self.conn.execute(f'INSERT INTO {ready.TABLE} VALUES(?,?,?,?,?)',(bundle,day,canonical(payload),day+'T12:00:00Z',day+'T12:00:00Z'))
    def select(self,dates=('2026-10-31','2026-11-02')):
        self.conn.commit();self.conn.execute('PRAGMA query_only=ON');self.conn.execute('BEGIN')
        return select_rehearsal_sources(self.conn,dates=dates,now=NOW)
    def mutate(self,sql,args=()):
        self.conn.rollback();self.conn.execute('PRAGMA query_only=OFF');self.conn.execute(sql,args);self.conn.commit()
    def test_unselected_large_archive_body_is_not_copied_but_header_change_is_fenced(self):
        self.put('2026-01-01',padding='x'*20000)
        with patch.object(publication,'MAX_BYTES',2000):selected=self.select()
        where,args=selected['filters'][ready.TABLE]
        copied=[dict(r) for r in self.conn.execute(f'SELECT * FROM {ready.TABLE} WHERE {where}',args)]
        self.assertEqual({r['as_of_date'] for r in copied},{'2026-10-31','2026-11-01'})
        self.assertEqual(selected['selected']['2026-11-02'],('b','2026-11-01'))
        fence=ready.pin_queries(self.conn,selected['selection_queries'])
        self.mutate(f'UPDATE {ready.TABLE} SET refreshed_at=? WHERE as_of_date=?',('2026-11-02T16:00:00Z','2026-01-01'))
        with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):ready.check_queries(self.conn,fence)
    def test_native_selection_is_identical_after_pruning_and_keeps_nonwinning_default(self):
        self.put('2026-11-02')
        selected=self.select(dates=['2026-10-31'])
        self.assertIn(('b','2026-11-01'),selected['ready_keys']) # actual default, although exact31 wins
        where,args=selected['filters'][ready.TABLE]
        values=[tuple(r) for r in self.conn.execute(f'SELECT * FROM {ready.TABLE} WHERE {where}',args)]
        with closing(sqlite3.connect(':memory:')) as dst:
            dst.row_factory=sqlite3.Row
            dst.execute('CREATE TABLE registry_upload_current_state(slot,bundle_version)');dst.execute("INSERT INTO registry_upload_current_state VALUES(1,'b')")
            dst.execute(f'CREATE TABLE {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,refreshed_at)')
            dst.executemany(f'INSERT INTO {ready.TABLE} VALUES(?,?,?,?,?)',values)
            rows,reselected=publication._selected_ready_rows(dst,plan={'scope':{'dates':['2026-10-31']}},authority=None,current_envelope=None,now=NOW)
            self.assertEqual(reselected,selected['selected'])
    def test_history_preserves_every_finalized_and_latest_capture_only(self):
        self.conn.executemany(f'INSERT INTO {CAPTURES_TABLE} VALUES(?,?,?)',[(1,'finalized','2026-10-31'),(2,'superseded','2026-10-31'),(3,'latest','2026-10-31'),(4,'other','2026-11-01')])
        self.conn.execute(f"INSERT INTO {FINALIZATIONS_TABLE} VALUES('finalized')")
        self.conn.executemany(f'INSERT INTO {COMPONENTS_TABLE} VALUES(?,?)',[(x,'all SKU/TOTAL native rows') for x in ('finalized','superseded','latest','other')])
        selected=self.select();where,args=selected['filters'][COMPONENTS_TABLE]
        self.assertEqual({r[0] for r in self.conn.execute(f'SELECT capture_id FROM {COMPONENTS_TABLE} WHERE {where}',args)},{'finalized','latest','other'})
        self.assertNotIn(CAPTURES_TABLE,selected['filters']);self.assertNotIn(FINALIZATIONS_TABLE,selected['filters'])
        fence=ready.pin_queries(self.conn,[(f'SELECT * FROM {COMPONENTS_TABLE} WHERE {where} ORDER BY capture_id',args)])
        self.mutate(f"INSERT INTO {FINALIZATIONS_TABLE} VALUES('superseded')")
        with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):ready.check_queries(self.conn,fence)
    def test_missing_date_and_oversized_selected_body_still_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'historical_exact_ready_dates_missing'):self.select(['2025-01-01'])
        self.conn.rollback();self.conn.execute('PRAGMA query_only=OFF')
        self.put('2026-10-30',padding='x'*10000)
        with patch.object(publication,'MAX_BYTES',2000),self.assertRaisesRegex(ValueError,'historical_ready_source_size_limit'):self.select(['2026-10-30'])
    def test_recovery_metadata_is_fenced_as_well_as_headers(self):
        from packages.application.web_vitrina_management_history import FACT_SOURCE,FACT_GROUPS
        day='2026-10-01';metadata={'server_cell_presentation':{'row':{day:{'source':FACT_SOURCE,'source_as_of_date':day,'complete_source_groups':sorted(FACT_GROUPS)}}}}
        self.put('2026-10-02',dates=[day],metadata=metadata)
        selected=self.select([day]);self.assertEqual(selected['selected'][day],('b','2026-10-02'))
        fence=ready.pin_queries(self.conn,selected['selection_queries'])
        self.mutate(f"UPDATE {ready.TABLE} SET plan_json=json_set(plan_json,'$.metadata.server_cell_presentation','changed') WHERE as_of_date='2026-10-02'")
        with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):ready.check_queries(self.conn,fence)
class RehearsalFirstWriteFenceTests(unittest.TestCase):
    def test_new_ready_exact_binding_blocks_precursor_before_first_native_effect(self):
        from apps import fbs_temporary_allocation_action_smoke as fixture_module
        from packages.application import fbs_temporary_allocation_action as action
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application.registry_upload_db_backed_runtime import _connect
        fixture=fixture_module.TemporaryAllocationFullActionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        directory=action._directory(fixture.root,'ready-fence-fixture')
        snapshot=action._snapshot(fixture.runtime,fixture.inputs,now=fixture.now)
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(fixture.runtime,request={},operation_id='ready-fence-fixture',directory=directory,snapshot=snapshot,inputs=fixture.inputs,now=fixture.now)
        recipe=action._make_recipe(fixture.runtime,snapshot=snapshot,unit=result['unit'],evidence=fixture.inputs[1],operation_id='ready-fence-fixture',created_at=result['unit']['native_plan']['posted_at'],numeric=result['numeric'],source_selection_fence=result['subset']['selection_fence'])
        with closing(_connect(fixture.runtime.db_path)) as conn:
            conn.execute(f"INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) SELECT bundle_version,'2026-10-27',plan_json,activated_at,'other-ready',plan_version,refreshed_at FROM {ready.TABLE} WHERE as_of_date='2026-11-02'")
            conn.commit()
        with warehouse_functional_job_lock(fixture.root) as owner:
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action._record_recipe(fixture.runtime,recipe,owner_token=owner['owner_token'])
        with ready.readonly(fixture.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {action.BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],5400)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_publications WHERE operation_id='ready-fence-fixture'").fetchone()[0],0)

class CachedPreflightCodeTests(unittest.TestCase):
    def test_both_cached_previews_reject_prior_code_before_attempt_or_effects(self):
        from tempfile import TemporaryDirectory
        from contextlib import nullcontext
        from types import SimpleNamespace
        from packages.application import fbs_historical_fbo_action as main3
        from packages.application import fbs_temporary_allocation_action as precursor
        from packages.application.fbs_snapshot_cost import fingerprint
        for module,service in ((main3,main3.HistoricalFboAction()),(precursor,precursor.TemporaryAllocationAction())):
            with self.subTest(action=module.RESTORE),TemporaryDirectory() as tmp:
                directory=Path(tmp);cached={'code_authority':{},'snapshot':{},'observed_at':NOW.isoformat()}
                cached['digest']=fingerprint(cached);main3._private(directory/'preflight.json',cached)
                runtime=SimpleNamespace(runtime_dir=directory,db_path=directory/'unused.sqlite3')
                with patch.object(module,'_inputs',return_value=()),patch.object(main3,'_runtime',return_value=runtime),patch.object(module,'_directory',return_value=directory),patch.object(main3,'_held',return_value=nullcontext({})),patch.object(module,'_snapshot',return_value={}),patch.object(module,'_preflight') as fresh:
                    with self.assertRaisesRegex(ValueError,'preflight_code_changed'):
                        service.preview({'action':module.RESTORE},'cached-operation',root=directory)
                    fresh.assert_not_called()
                self.assertFalse((directory/'attempt.json').exists())
                self.assertFalse(runtime.db_path.exists())

if __name__=='__main__':unittest.main()
