#!/usr/bin/env python3
"""Native owned unit/recovery tests; full WB/Ready integration is separate."""
from contextlib import closing,nullcontext
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import hashlib
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import ff_pool_fbs_historical_handoff_smoke as native_fixture
STAMP=native_fixture.STAMP
from packages.application import fbs_temporary_allocation_action as action
from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application import ff_pool_fbs_historical_handoff as handoffs
from packages.application.ff_pool_foundation import BALANCES_TABLE,OPERATIONS_TABLE
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def supplier_draft(preorder='1'):
    identity='preorder:'+preorder
    normalized=dict(supply_id=identity,cache_key=identity,preorder_id=preorder,wb_supply_id='',status_id=1,
        supply_date='',fact_date='',actual_warehouse_id='',accepted_quantity=0,unloading_quantity=0)
    listed=dict(preorderID=int(preorder),supplyID=None,statusID=1,supplyDate=None,factDate=None)
    detail=dict(statusID=1,supplyDate=None,factDate=None,actualWarehouseID=None,
        acceptedQuantity=0,unloadingQuantity=0,readyForSaleQuantity=0)
    goods=[dict(nmID=nm,quantity=100,acceptedQuantity=0,unloadingQuantity=0,readyForSaleQuantity=0) for nm in range(101,105)]
    return dict(supply_id=identity,cache_key=identity,preorder_id=preorder,wb_supply_id='',status_id=1,
        supply_date='',fact_date='',normalized_row_json=action.canonical(normalized),raw_list_json=action.canonical(listed),
        raw_detail_json=action.canonical(detail),raw_goods_json=action.canonical(goods),synced_at=STAMP)


def insert_supplier_draft(conn,preorder='1'):
    row=supplier_draft(preorder)
    conn.execute(f'INSERT INTO {action.P}wb_supplies({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
    return dict(conn.execute(f'SELECT * FROM {action.P}wb_supplies WHERE supply_id=?',(row['supply_id'],)).fetchone())


class SupplierDraftEvidenceTests(unittest.TestCase):
    def test_only_affirmatively_unshipped_drafts_are_nonphysical(self):
        row=supplier_draft();self.assertTrue(action._repair().is_unshipped_supplier_draft(row))
        for column,key,value in (
            ('raw_list_json','supplyID',123),('raw_list_json','preorderID',2),('raw_list_json','statusID',2),
            ('raw_list_json','supplyDate',STAMP),('raw_detail_json','factDate',STAMP),
            ('raw_detail_json','actualWarehouseID',1),('raw_detail_json','acceptedQuantity',1),
            ('raw_detail_json','unloadingQuantity',1),('raw_detail_json','readyForSaleQuantity',1),
            ('normalized_row_json','wb_supply_id','real'),('normalized_row_json','status_id',2),
        ):
            changed=deepcopy(row);value_json=action.json.loads(changed[column]);value_json[key]=value;changed[column]=action.canonical(value_json)
            with self.subTest(column=column,key=key):self.assertFalse(action._repair().is_unshipped_supplier_draft(changed))
        for key in ('actualWarehouseID','acceptedQuantity','unloadingQuantity','readyForSaleQuantity','supplyDate','factDate'):
            changed=deepcopy(row);value=action.json.loads(changed['raw_detail_json']);value.pop(key);changed['raw_detail_json']=action.canonical(value)
            self.assertFalse(action._repair().is_unshipped_supplier_draft(changed))
        for key in ('acceptedQuantity','unloadingQuantity','readyForSaleQuantity'):
            changed=deepcopy(row);goods=action.json.loads(changed['raw_goods_json']);goods[0][key]=1;changed['raw_goods_json']=action.canonical(goods)
            self.assertFalse(action._repair().is_unshipped_supplier_draft(changed))
            goods[0].pop(key);changed['raw_goods_json']=action.canonical(goods)
            self.assertFalse(action._repair().is_unshipped_supplier_draft(changed))


def replay_native_daily_projection(runtime, *, version_id):
    """Real native daily producer and its normal upsert on disposable rows."""
    from packages.application import warehouse_functional as wf
    from packages.application.warehouse_archival_estimate import overlay_opening_cost_rows
    with ready.readonly(runtime.db_path) as conn:
        version=dict(conn.execute(f'SELECT * FROM {action.stages.P}functional_versions WHERE version_id=?',(version_id,)).fetchone())
        snapshot=dict(conn.execute(f'SELECT * FROM {action.stages.P}wb_snapshots WHERE version_id=?',(version_id,)).fetchone())
        opening=[{**dict(row),'provenance':action.json.loads(row['provenance_json'])}
            for row in conn.execute(f'SELECT * FROM {action.stages.P}opening_cost_map WHERE cutover_id=?',(wf.FUNCTIONAL_CUTOVER_ID,))]
        rows=wf.WarehouseFunctionalBlock._build_post_cutover_daily_cost_projection(SimpleNamespace(_load_opening_cost_map=lambda: {}),
            captured_at=version['created_at'],candidate_lines=[],
            candidate_snapshot={**snapshot,'items':action.json.loads(snapshot['items_json'])},new_events=[],
            opening_cost_map=overlay_opening_cost_rows(conn,opening),cutover_mode=False,allow_partial=True,connection=conn)
    with closing(_connect(runtime.db_path)) as conn,conn:
        for item in rows:
            value=dict(cutover_id=wf.FUNCTIONAL_CUTOVER_ID,**{key:item[key] for key in ('as_of_date','nm_id','quantity','wac_rub','capital_rub','quality','fingerprint')},
                provenance_json=action.canonical(item['provenance']),created_at=version['created_at'])
            changed=[key for key in value if key not in {'cutover_id','as_of_date','nm_id'}]
            conn.execute(f'INSERT INTO {action.stages.P}wb_daily_cost({",".join(value)}) VALUES({",".join("?" for _ in value)}) '
                f'ON CONFLICT(cutover_id,as_of_date,nm_id) DO UPDATE SET {",".join(key+"=excluded."+key for key in changed)}',tuple(value.values()))
    return rows


class TemporaryAllocationCopyBudgetTests(unittest.TestCase):
    def test_measured_source_size_fits_but_each_safety_bound_still_blocks(self):
        action._check_source_copy_budget(table='source',count=50000,size=700*1024**2,elapsed=60)
        action._check_source_copy_budget(table='source',count=action.MAX_ROWS,size=action.MAX_BYTES,elapsed=119.9)
        for dimension,values in (
            ('rows',dict(count=action.MAX_ROWS+1,size=1,elapsed=0)),
            ('bytes',dict(count=1,size=action.MAX_BYTES+1,elapsed=0)),
            ('time',dict(count=1,size=1,elapsed=120)),
        ):
            with self.subTest(dimension=dimension):
                with self.assertRaisesRegex(ValueError,'source_copy_budget:'+dimension+':table=source:rows='):
                    action._check_source_copy_budget(table='source',**values)


class TemporaryCompletionClockTests(unittest.TestCase):
    def test_construction_and_approval_have_separate_exact_bounds(self):
        observed=datetime(2026,11,2,12,tzinfo=timezone.utc)
        authority=dict(contract=action.COMPLETION_CONTRACT,observed_at=observed.isoformat(),
            completed_at=(observed+timedelta(seconds=1800)).isoformat())
        action._completion_deadline(authority,now=observed+timedelta(seconds=2400))
        for offset in (2400.000001,1799.999999):
            with self.assertRaisesRegex(ValueError,'preflight_expired'):
                action._completion_deadline(authority,now=observed+timedelta(seconds=offset))
        authority['completed_at']=(observed+timedelta(seconds=1800.000001)).isoformat()
        with self.assertRaisesRegex(ValueError,'preflight_build_expired'):
            action._completion_deadline(authority,now=observed+timedelta(seconds=1801))
        authority.update(observed_at='2026-11-02T18:59:00+00:00',completed_at='2026-11-02T18:59:30+00:00')
        with self.assertRaisesRegex(ValueError,'business_day_changed'):
            action._completion_deadline(authority,now=datetime(2026,11,2,19,tzinfo=timezone.utc))

    def test_reviewed_files_are_reread_against_the_frozen_documentary_digests(self):
        source={'original_document_id':'accepted'};evidence={'evidence_digest':'reviewed'}
        snapshot=dict(documentary_digest=action.fingerprint(source),evidence_digest='reviewed')
        authority=dict(documentary_request={'retained_files':{'original':'retained'}})
        with patch.object(action,'_inputs',return_value=(source,evidence)) as read:
            action._completion_evidence(authority,snapshot=snapshot);read.assert_called_once_with(authority['documentary_request'])
        with patch.object(action,'_inputs',return_value=({'original_document_id':'other'},evidence)):
            with self.assertRaisesRegex(ValueError,'documentary_source_changed'):
                action._completion_evidence(authority,snapshot=snapshot)
        with patch.object(action,'_inputs',side_effect=ValueError('retained_source_changed')):
            with self.assertRaisesRegex(ValueError,'retained_source_changed'):
                action._completion_evidence(authority,snapshot=snapshot)


class TemporaryAllocationNativeTests(unittest.TestCase):
    def setUp(self):
        fixture=native_fixture.HistoricalHandoffTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture
        root=fixture.runtime.runtime_dir/'evidence'/'temporary-fbs-allocation'/'local-temp-unit'/'simulation';root.mkdir(parents=True)
        self.runtime=SimpleNamespace(runtime_dir=root,db_path=root/'registry_upload_runtime.sqlite3')
        with sqlite3.connect(fixture.pre27) as src,sqlite3.connect(self.runtime.db_path) as dst:src.backup(dst)
        self.book=deepcopy(fixture.book);self.book.update(shared_days={},wb_days={},retained_days={},presentations={});self.book['state']['observed_documents']={};self.version=accounting._save_book(root,self.book,expected=None,operation_id='local-initial')
        handoffs.load.return_value=(self.book,self.version)
        # Match the offset-form evidence export that blocked the real 583 preview.
        self.evidence=native_fixture.evidence_fixture(observed_at=STAMP.replace('Z','+00:00'))
        self.now=datetime.fromisoformat(STAMP.replace('Z','+00:00'))
        # This unit test isolates the actual native/T1 machinery. It does not
        # substitute a mock candidate or claim archived WB/Ready completion.
        with closing(_connect(self.runtime.db_path)) as conn:
            ready.ensure_publication_schema(conn)
            conn.execute('INSERT INTO registry_upload_versions VALUES(?,?,?)',('local-bundle',STAMP,STAMP))
            conn.execute(f'INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)',
                ('local-bundle','2026-11-02','{}',STAMP,'local','local',STAMP))
            conn.commit();conn.execute('BEGIN')
            original=handoffs._document(conn,'original07')
            self.snapshot=dict(book_version=self.version,original=original,facility_id='synthetic-moscow',nm_ids=[101,102,103,104],
                evidence_digest=self.evidence['evidence_digest'],documentary_digest='local',
                source_lines={str(row['nm_id']):row for row in original['lines']},
                native_guard=handoffs._capture_guard(conn,nms=[('synthetic-moscow',nm) for nm in range(101,105)],
                    order_ids=[row['order']['order_id'] for row in self.evidence['orders']]),
                ready_expected=asdict(ready.capture_expected(conn,bundle_version='local-bundle',as_of_date='2026-11-02',authority=ready.capture_authority(root))))
        with warehouse_functional_job_lock(root) as owner:
            self.unit=action._simulate_unit(self.runtime,snapshot=self.snapshot,evidence=self.evidence,operation_id='local-temp-unit',now=self.now,owner_token=owner['owner_token'])
        self.recipe=action._make_recipe(self.runtime,snapshot=self.snapshot,unit=self.unit,evidence=self.evidence,
            operation_id='local-temp-unit',created_at=STAMP,numeric=None)

    def apply(self,**kwargs):
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            binding=action._record_recipe(self.runtime,self.recipe,owner_token=owner['owner_token'])
            return action._apply_native_unit(self.runtime,self.recipe,binding=binding,owner_token=owner['owner_token'],**kwargs)

    def test_actual_native_T1_documents_whole_unit_and_idempotent_readback(self):
        self.assertTrue(self.apply());self.assertFalse(self.apply())
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertTrue(action._verify_unit(conn,self.unit))
            rows=list(conn.execute(f"SELECT pool,SUM(quantity),SUM(CAST(capital_rub AS REAL)) FROM {BALANCES_TABLE} GROUP BY pool"))
            self.assertEqual({r[0]:r[1] for r in rows},{'FBO':5368,'FBS':0})
            requests=list(conn.execute(f"SELECT state,recovery_operation_id FROM {action.docs.REQUESTS_TABLE} WHERE source_type=?",(action.EFFECT_CONTRACT,)))
            self.assertEqual(len(requests),2);self.assertTrue(all(row[0]=='complete' and row[1] for row in requests))
            operations=list(conn.execute("SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations WHERE operation_kind='ff_pool_document_posting'"))
            self.assertEqual([r[0] for r in operations],['retained','retained'])
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],32)
            observed=list(conn.execute(f"SELECT DISTINCT observed_at FROM {handoffs.orders.OBSERVATIONS_TABLE}"))
            self.assertEqual([row[0] for row in observed],[STAMP])
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {action.lifecycle.RECONCILIATION_TABLE} WHERE state='open'").fetchone()[0],1)

    def test_faults_roll_back_all_monetary_effects_then_same_recipe_recovers(self):
        for phase in ('after_allocation','after_handoffs','after_return'):
            def fail(boundary):
                if boundary==phase:raise RuntimeError('injected')
            with self.assertRaisesRegex(RuntimeError,'injected'):self.apply(fault=fail)
            with ready.readonly(self.runtime.db_path) as conn:
                self.assertFalse(action._verify_unit(conn,self.unit))
                self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBS'").fetchone()[0],0)
                self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],5400)
        self.assertTrue(self.apply());self.assertFalse(self.apply())

    def test_production_recipe_without_full_WB_candidate_cannot_admit_money(self):
        recipe=deepcopy(self.recipe);recipe['simulation_only']=False
        recipe['recipe_digest']=action.fingerprint({k:v for k,v in recipe.items() if k!='recipe_digest'})
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            with self.assertRaisesRegex(ValueError,'pre_native_source_fences_required'):
                action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],0)

    def test_cross_bound_recipe_and_ready_row_identity_are_rejected(self):
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            binding=action._record_recipe(self.runtime,self.recipe,owner_token=owner['owner_token'])
        for field,value in (('operation_id','foreign-operation'),('attempt_id','foreign-attempt'),
                            ('bundle_version','foreign-bundle'),('as_of_date','2026-11-01'),('expected_digest','sha256:'+'0'*64)):
            with self.subTest(field=field),closing(_connect(self.runtime.db_path)) as conn:
                conn.execute('BEGIN IMMEDIATE');changed_binding=deepcopy(binding)
                if field in {'operation_id','attempt_id'}:
                    # Keep a valid complete recipe hash: the independent
                    # row/binding identity must reject this cross-binding.
                    changed=deepcopy(self.recipe);changed[field]=value
                    changed['recipe_digest']=action.fingerprint({k:v for k,v in changed.items() if k!='recipe_digest'})
                    changed_binding['recipe_digest']=changed['recipe_digest']
                    conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE operation_id=? AND attempt_id=?',
                        (action.canonical(changed),binding['operation_id'],binding['attempt_id']))
                else:
                    conn.execute(f'UPDATE sheet_vitrina_v1_ready_publications SET {field}=? WHERE operation_id=? AND attempt_id=?',
                        (value,binding['operation_id'],binding['attempt_id']))
                with self.assertRaisesRegex(ValueError,'effect_recipe_identity_conflict'):
                    action._read_recipe(conn,changed_binding)
                conn.rollback()
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(action._read_recipe(conn,binding),self.recipe)
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],0)

    def test_interruption_after_native_commit_finishes_existing_T1_without_resend(self):
        with patch.object(action,'_finalize_native_documents',side_effect=OSError('lost_after_native_commit')):
            with self.assertRaisesRegex(OSError,'lost_after_native_commit'):self.apply()
        # These are the two explicit recovery phases called by _pipeline:
        # existing-money readback, then ordinary document finalization.
        self.assertFalse(self.apply())
        action._finalize_native_documents(self.runtime,self.unit)
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],32)
            states=[r[0] for r in conn.execute(f'SELECT state FROM {action.docs.REQUESTS_TABLE} WHERE source_type=?',(action.EFFECT_CONTRACT,))]
            self.assertEqual(states,['complete','complete'])




class TemporaryAllocationFullActionTests(unittest.TestCase):
    def setUp(self):
        from apps import fbs_temporary_allocation_repair_smoke as repair_fixture
        from packages.application.sheet_vitrina_v1_inventory_history import ensure_inventory_history_schema
        from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope,SheetVitrinaWriteTarget,SheetVitrinaV1TemporalSlot
        from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan
        fixture=repair_fixture.TemporaryRepairTests();fixture.prepare_only=True;fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.root=fixture.runtime.runtime_dir;target=self.root/'registry_upload_runtime.sqlite3'
        fixture.runtime.db_path.rename(target);fixture.runtime.db_path=target;self.runtime=fixture.runtime
        with closing(_connect(target)) as conn, conn:
            conn.execute('UPDATE sheet_vitrina_v1_ff_stock_operations SET source_file_blob=?',
                         (b'\x00original source attachment\xff',))
        self.book=deepcopy(fixture.book);self.book['effective_date']='2026-10-26'
        if self._testMethodName.startswith('test_closed_receipts_'):
            # Establish the accepted opening once, before its immutable save.
            self.book=repair_fixture.seed_closed_receipts_for_action(fixture,self.book)
            if self._testMethodName=='test_closed_receipts_registered_draft_continuation_never_repeats_native_and_completes_history':
                self.book['effective_date']=self.book['state']['baseline']['business_date']
        self.version=accounting._save_book(self.root,self.book,expected=None,operation_id='initial-source-book')
        handoffs.load.return_value=(self.book,self.version)
        self.now=datetime.fromisoformat(STAMP.replace('Z','+00:00'))
        self.wall_clock=patch.object(action,'_utc_now',return_value=self.now);self.wall_clock.start();self.addCleanup(self.wall_clock.stop)
        # External documentary parsing is owned by its primary-file tests.
        documentary=patch.object(action,'_completion_evidence');documentary.start();self.addCleanup(documentary.stop)
        days=repair_fixture.DAYS
        with closing(_connect(target)) as conn:
            # Exercise actual current-material publication, not the legacy
            # fixture's missing active-version shortcut.
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_active SET version_id='saved:2026-11-02'")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status VALUES(1,?,?,NULL,'saved:2026-11-02',?)",(STAMP,STAMP,STAMP))
            if self._testMethodName in {'test_ordinary_refresh_new_generation_then_history_finance_completes_descendant',
                    'test_closed_receipts_registered_draft_continuation_never_repeats_native_and_completes_history'}:
                # The ordinary native daily owner requires the genuine cutover
                # baseline; the narrower repair-only fixtures do not consume it.
                conn.execute(f'INSERT INTO {action.stages.P}functional_cutovers VALUES(?,?,?,?,?,?,?,?,?)',
                    ('warehouse_functional_cutover_v1','2026-10-26T00:00:00Z','posted',action.fingerprint('native-cutover'),'{}','{}','{}',STAMP,STAMP))
                conn.execute(f"UPDATE {action.stages.P}functional_versions SET version_kind='functional_cutover' WHERE version_id='saved:2026-10-26'")
            ensure_inventory_history_schema(conn);ready.ensure_publication_schema(conn);conn.commit();action.staging.ensure_staging_schema(conn)
        with closing(_connect(target)) as conn:
            conn.execute('INSERT INTO registry_upload_versions VALUES(?,?,?)',('local-bundle',STAMP,STAMP))
            conn.execute('INSERT INTO registry_upload_current_state VALUES(1,?,?)',('local-bundle',STAMP))
            for nm in range(101,105):conn.execute('INSERT INTO registry_upload_config_v2 VALUES(?,?,?,?,?,?)',('local-bundle',nm,1,'Synthetic','Synthetic',1))
            envelope=SheetVitrinaV1Envelope('local-bundle','original-ready','2026-11-02',days,
                [SheetVitrinaV1TemporalSlot(d,d,d) for d in days],{},[
                SheetVitrinaWriteTarget('DATA_VITRINA','A1','A1:K5','A:K','replace',False,['label','key',*days],
                    [['capital','SKU:'+str(nm)+'|own_capital_FF_capital_rub',*([999]*len(days))] for nm in range(101,105)],4,2+len(days)),
                SheetVitrinaWriteTarget('STATUS','A1','A1:B1','A:B','replace',False,['key','value'],[],0,2)])
            conn.execute(f'INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)',
                ('local-bundle','2026-11-02',_serialize_sheet_vitrina_plan(envelope),STAMP,'original-ready','original-plan',STAMP));conn.commit()
        self.inputs=(dict(original_document_id='original07',facility_id='synthetic-moscow',approval_reference='local-reviewed'),fixture.evidence)

    def _published_history_fixture(self):
        # The external UI document parser has its own primary-file checks; this
        # full numerical test starts with its exact normalized source result.
        directory=action._directory(self.root,'full-local-temporary')
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),operation_id='full-local-temporary',directory=directory,
                snapshot=snapshot,inputs=self.inputs,now=self.now)
        candidate=result['numeric']['candidate'];self.assertFalse(candidate['candidate_book'].get('publication_error'))
        self.assertEqual(len(candidate['initial_anchors']),3)
        self.assertEqual(candidate['quality_proof']['pending_post_handoff_cancellations'],[1001])
        from decimal import Decimal
        self.assertEqual(sum(Decimal(row['quantity']) for row in candidate['candidate_book']['shared_days']['2026-10-29']['rows'].values()),5382)
        # A private success never alters the real source book or its 5400 FBO.
        self.assertEqual(accounting.load(self.root)[1],self.version)
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],5400)
        sim=action.shared._runtime(directory/'simulation')
        from packages.application.fbs_accounting_historical_history import pending_receipt
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter,update_live_history
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.own_product_capital import OwnProductCapitalBlock
        import time
        OwnProductCapitalBlock(runtime=sim)
        receipt=pending_receipt(sim);self.assertIsNotNone(receipt)
        self.assertEqual(receipt.manifest['contract'],action.CONTRACT)
        receipt.freeze_scope(self.now);receipt.validate_sources_readonly(now=self.now)
        store=HistoryStore(directory/'history')
        adapter=LiveNativeAdapter(db_path=sim.db_path,runtime_dir=sim.runtime_dir,cache_dir=directory/'proofs',now=self.now,
            date_from='2026-10-27',date_to='2026-11-02',formula_epoch='local-temporary-fbs')
        result=update_live_history(adapter=adapter,runtime=sim,store=store,max_recomputes=31,
            deadline_monotonic=time.monotonic()+45,rolling14=False,group_blocks=True,backfill_dates=list(receipt.dates))
        self.assertIn(result['status'],{'published','unchanged'})
        proof=receipt.validate_native_readonly(adapter=adapter,store=store,now=self.now)
        self.assertEqual(set(proof['native']),set(receipt.dates))
        self.assertEqual(len(receipt.dates),6)
        return sim,receipt,proof

    def test_completion_revalidates_exact_own_changes_and_rejects_raw_source_drift(self):
        # Persist a real foreign BLOB before capture. Source hashing must use
        # the exact ready.pin_queries bytes representation without decoding it.
        with closing(_connect(self.runtime.db_path)) as conn,conn:
            ready.record_intent(conn,operation_id='foreign-blob',attempt_id='1',kind='local_blob_source',
                expected=None,inputs={},expected_book=None,book_required=False,ready_required=False,created_at=STAMP)
            conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE operation_id='foreign-blob'",
                (b'{"foreign":"original"}',))
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        directory=action._directory(self.root,'completion-local-temporary')
        completed=self.now+timedelta(seconds=650)
        action._utc_now.return_value=completed
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),
                operation_id='completion-local-temporary',directory=directory,snapshot=snapshot,inputs=self.inputs,now=self.now)
        self.assertEqual(result['observed_at'],self.now.isoformat())
        self.assertEqual(result['completion_authority']['completed_at'],completed.isoformat())
        recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],
            operation_id='completion-local-temporary',created_at=STAMP,numeric=result['numeric'],
            source_selection_fence=result['subset']['selection_fence'],completion_authority=result['completion_authority'])
        with warehouse_functional_job_lock(self.root) as owner:
            binding=action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
            action._admit_requests(self.runtime,recipe['unit']['inputs'],now=STAMP)
            with ready.readonly(self.runtime.db_path) as conn:
                action._validate_completion(self.runtime,authority=recipe['completion_authority'],snapshot=snapshot,
                    numeric=recipe['numeric'],file_authority=recipe['file_authority'],code_authority=recipe['adapter_code'],recipe=recipe,conn=conn)
            for table,column,value,where,args,reason in (
                (action.P+'ready_publications','inputs_json',b'{"foreign":"changed"}', 'operation_id=?',('foreign-blob',),'full_source_changed'),
                (action.P+'ready_publications','diagnostics_json','{"foreign":true}', 'operation_id=?',('completion-local-temporary',),'owned_recipe_changed'),
                (action.docs.REQUESTS_TABLE,'updated_at','changed','request_id=?',(recipe['unit']['ordinary'][0]['request']['request_id'],),'owned_request_changed'),
                (action.P+'warehouse_wb_snapshots','raw_rows_digest','changed','version_id=?',('saved:2026-10-31',),'source_changed'),
            ):
                with closing(_connect(self.runtime.db_path)) as conn:
                    previous=conn.execute(f'SELECT {column} FROM {table} WHERE {where}',args).fetchone()[0]
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE {where}',(value,*args));conn.commit()
                with self.assertRaisesRegex(ValueError,reason):
                    action._apply_native_unit(self.runtime,recipe,binding=binding,owner_token=owner['owner_token'])
                with closing(_connect(self.runtime.db_path)) as conn,conn:
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE {where}',(previous,*args))
            with patch.object(action,'_utc_now',return_value=completed+timedelta(seconds=601)):
                with self.assertRaisesRegex(ValueError,'preflight_expired'):
                    action._apply_native_unit(self.runtime,recipe,binding=binding,owner_token=owner['owner_token'])
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],5400)
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],0)
        with patch.object(action,'_utc_now',return_value=self.now+timedelta(seconds=1801)):
            with self.assertRaisesRegex(ValueError,'preflight_build_expired'):
                action._complete_preflight(self.runtime,result=result,inputs=self.inputs,operation_id='unused',request={})

    def test_completion_current_official_source_is_fresh_and_generation_exact(self):
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        with ready.readonly(self.runtime.db_path) as conn:
            from packages.application.fbs_snapshot_cost_sources import capture_current
            expected=capture_current(self.runtime.db_path,now=self.now,include_baseline=False,connection=conn)['quantity_snapshot']
        day=max(snapshot['source_versions']);numeric={'candidate':{'candidate_book':{'state':{'periods':{day:{'snapshot':expected}}}}}}
        with ready.readonly(self.runtime.db_path) as conn:
            stock=action._official_completion_stock(conn,snapshot=snapshot,numeric=numeric,now=self.now)
        self.assertEqual(stock['generation_id'],expected['id'])
        changed=deepcopy(numeric);changed['candidate']['candidate_book']['state']['periods'][day]['snapshot']['id']='other-generation'
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ValueError,'official_generation_changed'):
                action._official_completion_stock(conn,snapshot=snapshot,numeric=changed,now=self.now)
            with self.assertRaisesRegex(ValueError,'not_fresh'):
                action._official_completion_stock(conn,snapshot=snapshot,numeric=numeric,now=self.now+timedelta(minutes=31))
        with self.assertRaisesRegex(ValueError,'official_source_expired'):
            action._completion_stock_clock(stock,now=self.now+timedelta(minutes=31))

    def test_full_private_preflight_real_T1_material_WB_book_and_Ready(self):
        self._published_history_fixture()

    def test_supplier_rows_are_copied_pinned_and_physical_sources_reject_private_preflight(self):
        with closing(_connect(self.runtime.db_path)) as conn,conn:insert_supplier_draft(conn)
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        directory=action._directory(self.root,'supplier-copy');directory.mkdir(parents=True)
        destination=directory/'sources.sqlite3'
        with ready.readonly(self.runtime.db_path) as conn:
            copied=action._subset(conn,destination,book=self.book,now=self.now,snapshot=snapshot)
        self.assertEqual(copied['rows'][action.P+'wb_supplies'],1)
        with ready.readonly(destination) as conn:
            row=dict(conn.execute(f'SELECT * FROM {action.P}wb_supplies').fetchone())
            self.assertTrue(action._repair().is_unshipped_supplier_draft(row))
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),
                operation_id='draft-supplier',directory=action._directory(self.root,'draft-supplier'),snapshot=snapshot,inputs=self.inputs,now=self.now)
        self.assertFalse(result['numeric']['candidate']['candidate_book'].get('publication_error'))
        with closing(_connect(self.runtime.db_path)) as conn,conn:
            detail=action.json.loads(row['raw_detail_json']);detail['acceptedQuantity']=1
            conn.execute(f'UPDATE {action.P}wb_supplies SET raw_detail_json=?',(action.canonical(detail),))
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):action.stages.check_query_fence(conn,copied['query_fence'])
        with patch.object(action,'_retain',return_value={}):
            with self.assertRaisesRegex(ValueError,'selected_wb_supplier_source_present'):
                action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),
                    operation_id='physical-supplier',directory=action._directory(self.root,'physical-supplier'),snapshot=snapshot,inputs=self.inputs,now=self.now)

    def test_closed_receipts_registered_draft_continuation_never_repeats_native_and_completes_history(self):
        operation='legacy-draft-local';directory=action._directory(self.root,operation)
        legacy_code={**action._code(),'fbs_temporary_allocation_action.py':'sha256:legacy-fixture'}
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        with patch.object(action,'_code',return_value=legacy_code),patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),
                operation_id=operation,directory=directory,snapshot=snapshot,inputs=self.inputs,now=self.now)
            recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],operation_id=operation,
                created_at=STAMP,numeric=result['numeric'],source_selection_fence=result['subset']['selection_fence'],completion_authority=result['completion_authority'])
            with warehouse_functional_job_lock(self.root) as owner:
                binding=action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
                action._apply_native_unit(self.runtime,recipe,binding=binding,owner_token=owner['owner_token'])
                action._finalize_native_documents(self.runtime,recipe['unit'])
        saved=dict(recipe=recipe,binding=binding,request=dict(action=action.RESTORE))
        action.shared._private(directory/'attempt.json',saved)
        with closing(_connect(self.runtime.db_path)) as conn,conn:
            rows=[insert_supplier_draft(conn,str(i)) for i in (1,2,3)]
        registration=dict(operation_id=operation,original_code_digest=action.fingerprint(legacy_code),preorder_ids=['1','2','3'],
            row_digests={row['supply_id']:action.fingerprint(row) for row in rows})
        with ready.readonly(action.shared._runtime(directory/'simulation').db_path) as conn:
            stored=conn.execute(f'SELECT inputs_json FROM {action.P}ready_publications WHERE operation_id=?',(recipe['publication_operation_id'],)).fetchone()[0]
            registration.update(rehearsal_inputs_sha256='sha256:'+hashlib.sha256(stored.encode()).hexdigest(),
                rehearsal_manifest_digest=action.json.loads(stored)['manifest_digest'])
        with patch.dict(action.DRAFT_CONTINUATIONS,{recipe['recipe_digest']:registration}):
            manifest=action._prepare_draft_continuation(self.runtime,saved=saved,cached=result)
            self.assertEqual(action._numeric(manifest),recipe['numeric'])
            action.independent_rebuild(manifest,runtime_dir=self.root)
            self.assertEqual(accounting.load(self.root)[1],self.version)
            for kind in ('changed_row','fourth_row','other_source','native'):
                with closing(_connect(self.runtime.db_path)) as conn,conn:
                    if kind=='changed_row':conn.execute(f"UPDATE {action.P}wb_supplies SET synced_at='changed' WHERE preorder_id='1'")
                    elif kind=='fourth_row':insert_supplier_draft(conn,'4')
                    elif kind=='other_source':conn.execute(f"UPDATE {action.stages.P}wb_snapshots SET raw_rows_digest='changed' WHERE version_id='saved:2026-10-31'")
                    else:
                        native_document=recipe['unit']['ordinary'][0]['plan']['primary_document_id']
                        before=conn.execute(f'SELECT business_date FROM {action.docs.DOCUMENTS_TABLE} WHERE document_id=?',(native_document,)).fetchone()[0]
                        triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(action.docs.DOCUMENTS_TABLE,)).fetchall()
                        for trigger in triggers:conn.execute(f'DROP TRIGGER "{trigger[0]}"')
                        conn.execute(f'UPDATE {action.docs.DOCUMENTS_TABLE} SET business_date=? WHERE document_id=?',('2026-10-30',native_document))
                        for trigger in triggers:conn.execute(trigger[1])
                try:
                    with self.subTest(kind=kind),self.assertRaises(Exception):action.independent_rebuild(manifest,runtime_dir=self.root)
                finally:
                    with closing(_connect(self.runtime.db_path)) as conn,conn:
                        if kind=='changed_row':conn.execute(f'UPDATE {action.P}wb_supplies SET synced_at=? WHERE preorder_id=?',(STAMP,'1'))
                        elif kind=='fourth_row':conn.execute(f"DELETE FROM {action.P}wb_supplies WHERE preorder_id='4'")
                        elif kind=='other_source':
                            original=manifest['original_capture']['dates']['2026-10-31']['snapshot']['raw_rows_digest']
                            conn.execute(f"UPDATE {action.stages.P}wb_snapshots SET raw_rows_digest=? WHERE version_id='saved:2026-10-31'",(original,))
                        else:
                            for trigger in triggers:conn.execute(f'DROP TRIGGER "{trigger[0]}"')
                            conn.execute(f'UPDATE {action.docs.DOCUMENTS_TABLE} SET business_date=? WHERE document_id=?',(before,native_document))
                            for trigger in triggers:conn.execute(trigger[1])
            manifest=action._prepare_draft_continuation(self.runtime,saved=saved,cached=result)
            request=dict(action=action.RECOVER,recovery_ref=operation,original_recipe_digest=recipe['recipe_digest'])
            before_attempt=(directory/'attempt.json').read_bytes();before_preflight=(directory/'preflight.json').read_bytes()
            with patch.object(action.shared,'_held',return_value=nullcontext({})),patch.object(action,'_record_recipe',side_effect=AssertionError('native admission forbidden')),\
                    patch.object(action,'_apply_native_unit',side_effect=AssertionError('native write forbidden')),patch.object(action,'_finalize_native_documents',side_effect=AssertionError('native finalization forbidden')):
                with warehouse_functional_job_lock(self.root):
                    for crash_boundary in ('after_intent','after_book'):
                        with self.assertRaisesRegex(RuntimeError,'crash-after-child-intent'):
                            action.publication.publish(manifest,runtime_dir=self.root,
                                fault_injector=lambda boundary:(_ for _ in ()).throw(RuntimeError('crash-after-child-intent')) if boundary==crash_boundary else None)
                preview=action.TemporaryAllocationAction().preview(request,'continuation-local',root=self.root)
                with self.assertRaises(action.AmbiguousSubmit):action.TemporaryAllocationAction().apply(request,'continuation-local',preview,root=self.root)
            self.assertEqual((directory/'attempt.json').read_bytes(),before_attempt)
            self.assertEqual((directory/'preflight.json').read_bytes(),before_preflight)
            self.assertEqual(accounting.load(self.root)[1],recipe['numeric']['after_book'])
            from packages.application.fbs_accounting_historical_history import pending_receipt
            from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter,update_live_history
            from packages.application.web_vitrina_history_store import HistoryStore
            from packages.application.own_product_capital import OwnProductCapitalBlock
            from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
            from packages.application.business_data_heavy_admission import heavy_admitted
            import time
            historical_runtime=action.shared._runtime(self.root)
            OwnProductCapitalBlock(runtime=historical_runtime)
            receipt=pending_receipt(historical_runtime);self.assertIsNotNone(receipt)
            self.assertEqual(action._numeric(receipt.manifest),recipe['numeric']);receipt.freeze_scope(self.now)
            # The admitted historical absence proof survives later draft sync.
            with closing(_connect(self.runtime.db_path)) as conn,conn:conn.execute(f"UPDATE {action.P}wb_supplies SET synced_at='later-sync'")
            receipt.validate_sources_readonly(now=self.now)
            self._ordinary_refresh_history_finance_descendant(historical_runtime,receipt)

    def test_closed_receipts_full_private_preflight_history_and_pre_money_fence(self):
        from packages.application.fbs_accounting_historical_revision import _prefix
        from packages.application.fbs_accounting_historical_history import pending_receipt
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter,update_live_history
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.own_product_capital import OwnProductCapitalBlock
        import time
        before=action.fingerprint(_prefix(self.book,'2026-10-26'))
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        operation='closed-receipts-local-temporary';directory=action._directory(self.root,operation)
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),operation_id=operation,directory=directory,
                snapshot=snapshot,inputs=self.inputs,now=self.now)
        candidate=result['numeric']['candidate'];proof=candidate['quality_proof']['closed_prefix_repair']
        self.assertEqual(proof['immutable_prefix_digest'],before)
        self.assertEqual(action.fingerprint(_prefix(candidate['candidate_book'],'2026-10-26')),before)
        self.assertEqual(len(proof['receipt_document_ids']),2)
        self.assertEqual(min(candidate['editions']),'2026-10-26')
        self.assertFalse(candidate['candidate_book'].get('publication_error'))
        self.assertEqual(accounting.load(self.root)[1],self.version)
        sim=action.shared._runtime(directory/'simulation');OwnProductCapitalBlock(runtime=sim)
        receipt=pending_receipt(sim);self.assertIsNotNone(receipt)
        receipt.freeze_scope(self.now);receipt.validate_sources_readonly(now=self.now)
        store=HistoryStore(directory/'history')
        adapter=LiveNativeAdapter(db_path=sim.db_path,runtime_dir=sim.runtime_dir,cache_dir=directory/'proofs',now=self.now,
            date_from='2026-10-26',date_to='2026-11-02',formula_epoch='local-closed-receipts')
        history=update_live_history(adapter=adapter,runtime=sim,store=store,max_recomputes=31,
            deadline_monotonic=time.monotonic()+45,rolling14=False,group_blocks=True,backfill_dates=list(receipt.dates))
        self.assertIn(history['status'],{'published','unchanged'})
        native=receipt.validate_native_readonly(adapter=adapter,store=store,now=self.now)
        self.assertEqual(set(native['native']),set(receipt.dates));self.assertEqual(len(receipt.dates),7)
        recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],
            operation_id=operation,created_at=STAMP,numeric=result['numeric'],
            source_selection_fence=result['subset']['selection_fence'],completion_authority=result['completion_authority'])
        with warehouse_functional_job_lock(self.root) as owner:
            binding=action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
            with closing(_connect(self.runtime.db_path)) as conn,conn:
                with self.assertRaisesRegex(sqlite3.IntegrityError,'confirmed warehouse source is immutable'):
                    conn.execute("UPDATE sheet_vitrina_v1_ff_pool_operator_confirmations SET actor='changed-proof'")
                # Simulate out-of-band corruption in this disposable fixture;
                # restore the exact triggers before testing the independent fence.
                triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='sheet_vitrina_v1_ff_pool_operator_confirmations'").fetchall()
                for trigger in triggers:conn.execute(f'DROP TRIGGER "{trigger["name"]}"')
                conn.execute("UPDATE sheet_vitrina_v1_ff_pool_operator_confirmations SET actor='changed-proof'")
                for trigger in triggers:conn.execute(trigger['sql'])
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action._apply_native_unit(self.runtime,recipe,binding=binding,owner_token=owner['owner_token'])
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],0)
            self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBO' AND nm_id IN (101,102,103,104)").fetchone()[0],5400)

    def test_supplier_effective_date_does_not_replace_the_dated_wb_source(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            row=dict(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id='saved:2026-11-02'").fetchone())
            row.update(version_id='late-supplier-change',version_kind='targeted_supplier_replay',
                business_effective_date='2026-10-31',effective_at=STAMP,created_at=STAMP,
                source_watermarks_json=action.canonical(dict(base_version_id='saved:2026-11-02')),
                plan_fingerprint=action.fingerprint('late-supplier-change'))
            conn.execute(f'INSERT INTO sheet_vitrina_v1_warehouse_functional_versions({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
            conn.commit()
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        self.assertEqual(snapshot['source_versions']['2026-10-31'],'saved:2026-10-31')
        self.assertEqual(snapshot['targeted_version_ids'],['late-supplier-change'])
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET status='rolled_back' WHERE version_id='late-supplier-change'")
            conn.commit()
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action.stages.check_query_fence(conn,snapshot['selector_fence'])

    def test_corrupt_ordinary_dated_source_is_not_silently_skipped(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET raw_rows_digest='corrupt' WHERE version_id='saved:2026-10-31'")
            conn.commit()
        with self.assertRaisesRegex(ValueError,'snapshot_quantity_authority_invalid'):
            action._snapshot(self.runtime,self.inputs,now=self.now)

    def test_actual_finalizer_accepts_only_the_six_closed_history_days(self):
        sim,receipt,proof=self._published_history_fixture()
        manifest=receipt.manifest
        self.assertEqual(len(manifest['candidate']['editions']),7)
        self.assertEqual(set(proof['native']),{day for day in manifest['plan']['scope']['dates'] if day<manifest['plan']['scope']['date_to']})
        ack=dict(manifest_digest=manifest['manifest_digest'],native=proof['native'])
        from datetime import timedelta
        tomorrow=self.now+timedelta(days=1)
        canonical_next_day=set(receipt.freeze_scope(tomorrow).dates)
        self.assertEqual(len(canonical_next_day),7)
        self.assertEqual(action._history_ack_dates(manifest['plan']['scope'],now=tomorrow),canonical_next_day)
        receipt.freeze_scope(self.now)
        # This test exercises the domain finalizer after the owned-History
        # handoff. Its actual native compiler proof is persisted at that
        # boundary; Linux supervisor/FD admission has its separate tests.
        with closing(_connect(sim.db_path)) as conn:
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (manifest['operation_id'],manifest['attempt_id'])).fetchone()
            diagnostics=action.json.loads(row[0]);diagnostics['historical_history_ack']=ack
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',
                (action.canonical(diagnostics),manifest['operation_id'],manifest['attempt_id']));conn.commit()
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        finance=WbFinanceWeeklyBlock(sim.runtime_dir,now_factory=lambda:self.now)
        before=accounting.load(sim.runtime_dir)[1]
        flags=finance.recalculate_stale_cost_weeks()
        flags['accounting_version_unchanged']=before==accounting.load(sim.runtime_dir)[1]
        flags.update(accounting_version_before=before,accounting_version=before)
        economics=accounting.current_publication_receipt(sim,now=self.now)
        from packages.application.business_data_heavy_admission import heavy_admitted
        with heavy_admitted(sim.runtime_dir,operation='cycle'),warehouse_functional_job_lock(sim.runtime_dir):
            bound=accounting.load(sim.runtime_dir)
            with patch.object(action.accounting,'load',return_value=(bound[0],'later-book-version')):
                with self.assertRaisesRegex(ValueError,'original_book_revision_changed'):
                    action.finalize_scope(sim,manifest=manifest,history_ack=ack,finance=flags,economics=economics,now=self.now)
            target=manifest['targets'][-1]['expected']
            with closing(_connect(sim.db_path)) as conn:
                saved=conn.execute(f'SELECT plan_json FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',
                    (target['bundle_version'],target['as_of_date'])).fetchone()[0]
                conn.execute(f'UPDATE {ready.TABLE} SET plan_json=? WHERE bundle_version=? AND as_of_date=?',
                    ('{}',target['bundle_version'],target['as_of_date']));conn.commit()
            try:
                with self.assertRaisesRegex(ValueError,'current_accounting_ready_publication_mismatch'):
                    action.finalize_scope(sim,manifest=manifest,history_ack=ack,finance=flags,economics=economics,now=self.now)
            finally:
                with closing(_connect(sim.db_path)) as conn:
                    conn.execute(f'UPDATE {ready.TABLE} SET plan_json=? WHERE bundle_version=? AND as_of_date=?',
                        (saved,target['bundle_version'],target['as_of_date']));conn.commit()
            for invalid in ('missing_closed_day','includes_current_day'):
                changed=deepcopy(ack)
                if invalid=='missing_closed_day':changed['native'].pop(min(changed['native']))
                else:changed['native'][manifest['plan']['scope']['date_to']]={}
                with self.assertRaisesRegex(ValueError,'exact_history_ack_required'):
                    action.finalize_scope(sim,manifest=manifest,history_ack=changed,finance=flags,economics=economics,now=self.now)
                with ready.readonly(sim.db_path) as conn:
                    binding=manifest['plan']['effect_binding']
                    self.assertEqual(conn.execute('SELECT state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                        (binding['operation_id'],binding['attempt_id'])).fetchone()[0],'prepared')
            result=action.finalize_scope(sim,manifest=manifest,history_ack=ack,finance=flags,economics=economics,now=self.now)
        self.assertEqual(result['status'],'scope_complete')
        self.assertFalse(result['operator_receipts_completed'])
        from packages.application.fbs_accounting_historical_history import pending_receipt
        self.assertIsNone(pending_receipt(sim))
        with ready.readonly(sim.db_path) as conn:
            binding=manifest['plan']['effect_binding']
            row=conn.execute('SELECT state,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (binding['operation_id'],binding['attempt_id'])).fetchone()
            self.assertEqual(row[0],'complete')
            stored=action.json.loads(row[1])['temporary_allocation_completion']['history_ack']
            self.assertEqual(set(stored['native']),set(receipt.dates))
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {action.lifecycle.RECONCILIATION_TABLE} WHERE state='open'").fetchone()[0],1)

    def test_ordinary_refresh_new_generation_then_history_finance_completes_descendant(self):
        sim,receipt,_=self._published_history_fixture()
        self._ordinary_refresh_history_finance_descendant(sim,receipt)

    def _ordinary_refresh_history_finance_descendant(self,sim,receipt):
        from packages.application import business_data_heavy_admission as admission
        from packages.application import registry_upload_db_backed_runtime as db_runtime
        from packages.application import fbs_accounting_historical_cycle as historical_cycle
        from packages.application import fbs_accounting_historical_history as historical_history
        from packages.application import warehouse_functional as wf
        from packages.application.warehouse_business_projection import publish_functional_version_business_projection
        from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter,update_live_history
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages import business_time
        from apps.warehouse_functional_runner import _publish_fbs_snapshot_accounting,_recalculate_downstream_finance_cost
        manifest=receipt.manifest
        later=self.now+timedelta(minutes=1);stamp=later.isoformat().replace('+00:00','Z');day=business_time.current_business_date_iso(later)
        original=deepcopy(accounting.load(sim.runtime_dir)[0]);old_version=manifest['after_book']
        middle=deepcopy(original);middle['prepared_at']=(self.now+timedelta(seconds=30)).isoformat()
        middle_version=accounting._save_book(sim.runtime_dir,middle,expected=old_version,operation_id='local-middle-refresh')
        with closing(_connect(sim.db_path)) as conn,conn:
            # Actual new official registry/stock rows, preserving the complete
            # catalog and facility identities while changing generation/time.
            old=original['state']['periods'][day]['snapshot']['id']
            generation=dict(conn.execute(f'SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?',(old,)).fetchone())
            generation.update(run_id='ordinary-official',run_sequence=conn.execute(f'SELECT MAX(run_sequence)+1 FROM {REGISTRY_RUNS_TABLE}').fetchone()[0],
                started_at=stamp,completed_at=stamp,generation_digest=action.fingerprint('ordinary-generation'))
            conn.execute(f'INSERT INTO {REGISTRY_RUNS_TABLE}({",".join(generation)}) VALUES({",".join("?" for _ in generation)})',tuple(generation.values()))
            for stock in conn.execute(f'SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=?',(old,)).fetchall():
                fresh=dict(stock);fresh.update(run_id='ordinary-'+stock['run_id'],registry_run_id='ordinary-official',snapshot_at=stamp,
                    run_sequence=conn.execute(f'SELECT MAX(run_sequence)+1 FROM {STOCK_RUNS_TABLE}').fetchone()[0])
                conn.execute(f'INSERT INTO {STOCK_RUNS_TABLE}({",".join(fresh)}) VALUES({",".join("?" for _ in fresh)})',tuple(fresh.values()))
                for row in conn.execute(f'SELECT * FROM {STOCK_ROWS_TABLE} WHERE run_id=?',(stock['run_id'],)).fetchall():
                    value=dict(row);value['run_id']=fresh['run_id']
                    conn.execute(f'INSERT INTO {STOCK_ROWS_TABLE}({",".join(value)}) VALUES({",".join("?" for _ in value)})',tuple(value.values()))
            edition=manifest['candidate']['editions'][day];new='ordinary-functional-current'
            version=deepcopy(edition['version']);version.update(version_id=new,version_kind='hourly_wb_sync',created_at=stamp,published_at=stamp,effective_at=stamp,
                plan_fingerprint=action.fingerprint(new))
            snapshot=deepcopy(edition['snapshot']);watermark=action.json.loads(version['source_watermarks_json'])
            watermark['captured_at']=stamp;version['source_watermarks_json']=action.canonical(watermark)
            snapshot.update(version_id=new,created_at=stamp,snapshot_id=wf._stable_id('wbsnapv',dict(source_snapshot_id=watermark['wb_snapshot']['snapshot_id'],version_id=new)))
            for table,value in (('functional_versions',version),('wb_snapshots',snapshot)):
                conn.execute(f'INSERT INTO {action.stages.P}{table}({",".join(value)}) VALUES({",".join("?" for _ in value)})',tuple(value.values()))
            rows=[]
            for raw in conn.execute(f'SELECT * FROM {action.stages.P}functional_balances WHERE version_id=?',(edition['version_id'],)).fetchall():
                value=dict(raw);value['version_id']=new
                conn.execute(f'INSERT INTO {action.stages.P}functional_balances({",".join(value)}) VALUES({",".join("?" for _ in value)})',tuple(value.values()))
                value['provenance']=action.json.loads(value.pop('provenance_json'));rows.append(value)
            conn.execute(f'UPDATE {action.stages.P}functional_active SET version_id=?',(new,))
            wf._materialize_compact_warehouse_read_models(conn,version_id=new,plan={'lines':rows},created_at=stamp,effective_at=stamp,business_effective_date=day)
            publish_functional_version_business_projection(conn,published_version_id=new,business_effective_date=day,published_at=stamp,source_revision=action.fingerprint(new))
            # The normal cycle already has its default dated Ready envelope.
            # Use a different key from the immutable historical child target.
            from dataclasses import replace
            default=business_time.default_business_as_of_date(later)
            old_ready=conn.execute(f'SELECT * FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',
                ('local-bundle',day)).fetchone()
            envelope=db_runtime._deserialize_sheet_vitrina_plan(old_ready['plan_json'])
            value=dict(old_ready);value.update(as_of_date=default,
                plan_json=db_runtime._serialize_sheet_vitrina_plan(replace(envelope,as_of_date=default)))
            # The bounded rehearsal does not consume this registry header;
            # ordinary Ready's FK-enforcing writer needs its real parent row.
            conn.execute('INSERT OR IGNORE INTO registry_upload_versions VALUES(?,?,?)',('local-bundle',STAMP,STAMP))
            conn.execute(f'INSERT INTO {ready.TABLE}({",".join(value)}) VALUES({",".join("?" for _ in value)})',tuple(value.values()))
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return later if tz else later.replace(tzinfo=None)
        original_business_day=business_time.current_business_date_iso
        replayed=replay_native_daily_projection(sim,version_id=new)
        self.assertTrue(replayed)
        old_daily={(row['as_of_date'],row['nm_id']):row for row in manifest['candidate']['daily_wb_costs'] if row['as_of_date']<day}
        self.assertTrue(any((row['as_of_date'],row['nm_id']) in old_daily and
            row['fingerprint']!=old_daily[(row['as_of_date'],row['nm_id'])]['fingerprint'] for row in replayed))
        with patch.object(accounting,'datetime',Clock),patch.object(db_runtime,'datetime',Clock),patch.object(historical_cycle,'datetime',Clock),patch.object(business_time,'current_business_date_iso',side_effect=lambda now=None:original_business_day(now or later)):
            with warehouse_functional_job_lock(sim.runtime_dir):
                refreshed=_publish_fbs_snapshot_accounting(sim)
        current,version=accounting.load(sim.runtime_dir)
        self.assertEqual(refreshed['status'],'published');self.assertNotEqual(version,old_version)
        self.assertNotEqual(current['prepared_at'],original['prepared_at'])
        self.assertEqual(current['state']['periods'][day]['snapshot']['id'],'ordinary-official')
        self.assertEqual(accounting.load(sim.runtime_dir,version=old_version)[0],original)
        action._completion_books(sim,manifest=manifest)
        with ready.readonly(sim.db_path) as conn:
            action._verify_closed_daily_projection(conn,manifest=manifest,current_book=current)
        frozen=manifest['candidate']['editions'][min(manifest['candidate']['editions'])]
        with closing(_connect(sim.db_path)) as conn,conn:
            conn.execute(f'UPDATE {action.stages.P}wb_snapshots SET raw_rows_json=? WHERE version_id=?',
                ('[{"foreign":true}]',frozen['version_id']))
        try:
            with ready.readonly(sim.db_path) as conn:
                with self.assertRaisesRegex(ValueError,'original_stage_snapshot_changed'):
                    action._verify_original_publication(conn,manifest=manifest,current_book=current)
        finally:
            with closing(_connect(sim.db_path)) as conn,conn:
                conn.execute(f'UPDATE {action.stages.P}wb_snapshots SET raw_rows_json=? WHERE version_id=?',
                    (frozen['snapshot']['raw_rows_json'],frozen['version_id']))
        selected=next(row for row in manifest['candidate']['daily_wb_costs'] if row['as_of_date']<day)
        daily_where=(selected['as_of_date'],selected['nm_id'])
        for column,value,reason in (('quantity','999','closed_daily_money_changed'),
                                   ('wac_rub','999','closed_daily_money_changed'),
                                   ('capital_rub','999','closed_daily_money_changed'),
                                   ('quality','foreign-quality','closed_daily_money_changed'),
                                   ('created_at','foreign-time','closed_daily_writer_changed'),
                                   ('fingerprint','foreign-digest','closed_daily_native_replay_changed'),
                                   ('provenance_json','{}','closed_daily_native_replay_changed')):
            with closing(_connect(sim.db_path)) as conn,conn:
                saved=conn.execute(f'SELECT {column} FROM {action.stages.P}wb_daily_cost WHERE as_of_date=? AND nm_id=?',daily_where).fetchone()[0]
                old_fingerprint=conn.execute(f'SELECT fingerprint FROM {action.stages.P}wb_daily_cost WHERE as_of_date=? AND nm_id=?',daily_where).fetchone()[0]
                conn.execute(f'UPDATE {action.stages.P}wb_daily_cost SET {column}=? WHERE as_of_date=? AND nm_id=?',(value,*daily_where))
                if column=='provenance_json':
                    raw=conn.execute(f'SELECT * FROM {action.stages.P}wb_daily_cost WHERE as_of_date=? AND nm_id=?',daily_where).fetchone()
                    forged=wf._daily_wb_cost_row(day=raw['as_of_date'],nm_id=raw['nm_id'],quantity=wf._decimal(raw['quantity']),
                        wac=wf._decimal(raw['wac_rub']),quality=raw['quality'],provenance={})
                    conn.execute(f'UPDATE {action.stages.P}wb_daily_cost SET fingerprint=? WHERE as_of_date=? AND nm_id=?',(forged['fingerprint'],*daily_where))
            try:
                with ready.readonly(sim.db_path) as conn:
                    with self.assertRaisesRegex(ValueError,reason):action._verify_closed_daily_projection(conn,manifest=manifest,current_book=current)
            finally:
                with closing(_connect(sim.db_path)) as conn,conn:
                    conn.execute(f'UPDATE {action.stages.P}wb_daily_cost SET {column}=?,fingerprint=? WHERE as_of_date=? AND nm_id=?',(saved,old_fingerprint,*daily_where))
        # Even a valid current hash cannot inherit through a corrupt middle.
        with sqlite3.connect(accounting.path(sim.runtime_dir)) as ledger:
            payload=ledger.execute('SELECT payload FROM accounting_revisions WHERE version=?',(middle_version,)).fetchone()[0]
            damaged=action.json.loads(payload);damaged['prepared_at']='corrupt-middle'
            ledger.execute('UPDATE accounting_revisions SET payload=? WHERE version=?',(action.canonical(damaged),middle_version))
        try:
            with self.assertRaisesRegex(ValueError,'book_corrupt'):action._completion_books(sim,manifest=manifest)
        finally:
            with sqlite3.connect(accounting.path(sim.runtime_dir)) as ledger:
                ledger.execute('UPDATE accounting_revisions SET payload=? WHERE version=?',(payload,middle_version))
        closed=min(d for d,p in original['state']['periods'].items() if p['status']=='closed')
        for field in ('quantity','capital_rub'):
            with self.subTest(closed_operand=field):
                damaged=deepcopy(current);damaged['wb_days'][closed]['rows'][0][field]='999'
                accounting._save_book(sim.runtime_dir,damaged,expected=version,operation_id='changed-closed-'+field)
                try:
                    with self.assertRaisesRegex(ValueError,'closed_book_prefix_changed'):action._completion_books(sim,manifest=manifest)
                finally:
                    with sqlite3.connect(accounting.path(sim.runtime_dir)) as ledger:
                        ledger.execute('UPDATE accounting_current SET version=? WHERE singleton=1',(version,))
        with closing(_connect(sim.db_path)) as conn,conn:
            doc=manifest['plan']['effect_binding']
            recipe=action._read_recipe(conn,doc);identity=recipe['unit']['ordinary'][0]['plan']['primary_document_id']
            saved=conn.execute(f'SELECT posted_manifest_sha256 FROM {action.docs.DOCUMENTS_TABLE} WHERE document_id=?',(identity,)).fetchone()[0]
            triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(action.docs.DOCUMENTS_TABLE,)).fetchall()
            for trigger in triggers:conn.execute(f'DROP TRIGGER "{trigger[0]}"')
            conn.execute(f'UPDATE {action.docs.DOCUMENTS_TABLE} SET posted_manifest_sha256=? WHERE document_id=?',('changed-native-receipt',identity))
            for trigger in triggers:conn.execute(trigger[1])
        try:
            with ready.readonly(sim.db_path) as conn:
                with self.assertRaisesRegex(ValueError,'manifest_corrupt'):action._verify_original_publication(conn,manifest=manifest,current_book=current)
        finally:
            with closing(_connect(sim.db_path)) as conn,conn:
                for trigger in triggers:conn.execute(f'DROP TRIGGER "{trigger[0]}"')
                conn.execute(f'UPDATE {action.docs.DOCUMENTS_TABLE} SET posted_manifest_sha256=? WHERE document_id=?',(saved,identity))
                for trigger in triggers:conn.execute(trigger[1])
        receipt=historical_history.pending_receipt(sim).freeze_scope(later);receipt.validate_sources_readonly(now=later)
        directory=self.root.parent/'ordinary-descendant-proof'
        store=HistoryStore(directory/'history')
        adapter=LiveNativeAdapter(db_path=sim.db_path,runtime_dir=sim.runtime_dir,cache_dir=directory/'descendant-proofs',now=later,
            date_from=min(manifest['plan']['scope']['dates']),date_to=day,formula_epoch='local-temporary-fbs')
        updated=update_live_history(adapter=adapter,runtime=sim,store=store,max_recomputes=31,
            deadline_monotonic=action.time.monotonic()+45,rolling14=False,group_blocks=True,backfill_dates=list(receipt.dates))
        self.assertIn(updated['status'],{'published','unchanged'})
        native=receipt.validate_native_readonly(adapter=adapter,store=store,now=later)
        ack=dict(manifest_digest=manifest['manifest_digest'],native=native['native'])
        with closing(_connect(sim.db_path)) as conn,conn:
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(manifest['operation_id'],manifest['attempt_id'])).fetchone()
            diagnostics=action.json.loads(row[0]);diagnostics['historical_history_ack']=ack
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',(action.canonical(diagnostics),manifest['operation_id'],manifest['attempt_id']))
        with admission.heavy_admitted(sim.runtime_dir,operation='cycle'),warehouse_functional_job_lock(sim.runtime_dir):
            finance=_recalculate_downstream_finance_cost(sim,now=later)
            economics=accounting.current_publication_receipt(sim,now=later)
            result=action.finalize_scope(sim,manifest=manifest,history_ack=ack,finance=finance,economics=economics,now=later)
        self.assertEqual(result['status'],'scope_complete');self.assertEqual(result['current_completion_book'],version)
        self.assertEqual(result['historical_after_book'],old_version)
        self.assertIsNone(historical_history.pending_receipt(sim))
        self.assertEqual(accounting.current_publication_receipt(sim,now=later),economics)
        with ready.readonly(sim.db_path) as conn:
            parent=conn.execute('SELECT book_version,after_digest,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                tuple(manifest['plan']['effect_binding'][key] for key in ('operation_id','attempt_id'))).fetchone()
            self.assertEqual(parent[0],old_version);self.assertEqual(parent[1],ready.digest(manifest['targets'][-1]['after_json']))
            proof=action.json.loads(parent[2])['temporary_allocation_completion']
            self.assertEqual(proof['current_completion_book'],version);self.assertEqual(proof['historical_after_book'],old_version)
            action._verify_original_publication(conn,manifest=manifest,current_book=current)

    def test_pre_native_selector_drift_blocks_the_first_monetary_write(self):
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        directory=action._directory(self.root,'selector-local-temporary')
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),operation_id='selector-local-temporary',directory=directory,
                snapshot=snapshot,inputs=self.inputs,now=self.now)
        recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],
            operation_id='selector-local-temporary',created_at=STAMP,numeric=result['numeric'],
            source_selection_fence=result['subset']['selection_fence'],completion_authority=result['completion_authority'])
        self.assertTrue(recipe['source_selection_fence'])
        with warehouse_functional_job_lock(self.root) as owner:
            unpriced=deepcopy(recipe);unpriced['numeric']['candidate']['initial_anchors']={}
            unpriced['recipe_digest']=action.fingerprint({k:v for k,v in unpriced.items() if k!='recipe_digest'})
            with self.assertRaisesRegex(ValueError,'full_healthy_preflight_required_before_effects'):
                action._record_recipe(self.runtime,unpriced,owner_token=owner['owner_token'])
            binding=action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
            with closing(_connect(self.runtime.db_path)) as conn:
                row=dict(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id='saved:2026-11-02'").fetchone())
                row.update(version_id='foreign-newer-source',effective_at='2026-11-02T15:01:00Z',created_at='2026-11-02T15:01:00Z')
                row['plan_fingerprint']=action.fingerprint('foreign-newer-source')
                conn.execute(f'INSERT INTO sheet_vitrina_v1_warehouse_functional_versions({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()));conn.commit()
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action._apply_native_unit(self.runtime,recipe,binding=binding,owner_token=owner['owner_token'])
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],5400)
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],0)

    def test_continuation_rechecks_foreign_operands_before_any_money(self):
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        directory=action._directory(self.root,'foreign-fence-local-temporary')
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request=dict(evidence_manifest_path='fixture',evidence_manifest_sha256='fixture'),operation_id='foreign-fence-local-temporary',directory=directory,
                snapshot=snapshot,inputs=self.inputs,now=self.now)
        # Exercise the recovery boundary with an immutable source proof whose
        # selector and selected native prestate remain unchanged.
        with ready.readonly(self.runtime.db_path) as conn:
            result['numeric']['candidate']['foreign_source_fence']=action.json.loads(action.canonical(ready.pin_queries(conn,[
                ('SELECT nomenclature_name FROM sheet_vitrina_v1_nomenclature_items WHERE nm_id=?',(101,))])))
        recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],
            operation_id='foreign-fence-local-temporary',created_at=STAMP,numeric=result['numeric'],
            source_selection_fence=result['subset']['selection_fence'],completion_authority=result['completion_authority'])
        with warehouse_functional_job_lock(self.root) as owner:
            binding=action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
            with closing(_connect(self.runtime.db_path)) as conn,conn:
                conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET nomenclature_name='changed-source' WHERE nm_id=101")
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action._record_recipe(self.runtime,recipe,owner_token=owner['owner_token'])
            with self.assertRaisesRegex(Exception,'ready_repair_source_changed'):
                action._apply_native_unit(self.runtime,recipe,binding=binding,owner_token=owner['owner_token'])
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT SUM(quantity) FROM {BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],5400)
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
