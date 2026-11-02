#!/usr/bin/env python3
"""Native owned unit/recovery tests; full WB/Ready integration is separate."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
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
        self.version=accounting._save_book(self.root,self.book,expected=None,operation_id='initial-source-book')
        handoffs.load.return_value=(self.book,self.version)
        self.now=datetime.fromisoformat(STAMP.replace('Z','+00:00'))
        days=repair_fixture.DAYS
        with closing(_connect(target)) as conn:
            # Exercise actual current-material publication, not the legacy
            # fixture's missing active-version shortcut.
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_active SET version_id='saved:2026-11-02'")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status VALUES(1,?,?,NULL,'saved:2026-11-02',?)",(STAMP,STAMP,STAMP))
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
            result=action._preflight(self.runtime,request={},operation_id='full-local-temporary',directory=directory,
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

    def test_full_private_preflight_real_T1_material_WB_book_and_Ready(self):
        self._published_history_fixture()

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
        economics=accounting.current_publication_receipt(sim,now=self.now)
        from packages.application.business_data_heavy_admission import heavy_admitted
        with heavy_admitted(sim.runtime_dir,operation='cycle'),warehouse_functional_job_lock(sim.runtime_dir):
            bound=accounting.load(sim.runtime_dir)
            with patch.object(action.accounting,'load',return_value=(bound[0],'later-book-version')):
                with self.assertRaisesRegex(ValueError,'healthy_published_book_required'):
                    action.finalize_scope(sim,manifest=manifest,history_ack=ack,finance=flags,economics=economics,now=tomorrow)
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

    def test_pre_native_selector_drift_blocks_the_first_monetary_write(self):
        snapshot=action._snapshot(self.runtime,self.inputs,now=self.now)
        directory=action._directory(self.root,'selector-local-temporary')
        with patch.object(action,'_retain',return_value={}):
            result=action._preflight(self.runtime,request={},operation_id='selector-local-temporary',directory=directory,
                snapshot=snapshot,inputs=self.inputs,now=self.now)
        recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],
            operation_id='selector-local-temporary',created_at=STAMP,numeric=result['numeric'],
            source_selection_fence=result['subset']['selection_fence'])
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
            result=action._preflight(self.runtime,request={},operation_id='foreign-fence-local-temporary',directory=directory,
                snapshot=snapshot,inputs=self.inputs,now=self.now)
        # Exercise the recovery boundary with an immutable source proof whose
        # selector and selected native prestate remain unchanged.
        with ready.readonly(self.runtime.db_path) as conn:
            result['numeric']['candidate']['foreign_source_fence']=action.json.loads(action.canonical(ready.pin_queries(conn,[
                ('SELECT nomenclature_name FROM sheet_vitrina_v1_nomenclature_items WHERE nm_id=?',(101,))])))
        recipe=action._make_recipe(self.runtime,snapshot=snapshot,unit=result['unit'],evidence=self.inputs[1],
            operation_id='foreign-fence-local-temporary',created_at=STAMP,numeric=result['numeric'],
            source_selection_fence=result['subset']['selection_fence'])
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
