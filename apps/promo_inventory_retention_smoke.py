"""Real promo publication/repair -> accepted inventory reader -> native day cells."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import apps.web_vitrina_management_inventory_history_smoke as inventory_fixture
from apps.promo_archive_publication import (PromoArchivePublicationAdapter, SKU_METRICS,
    TOTAL_METRICS, rollback_apply, rollback_preview, _connect, LEDGER)
from apps.production_apply_contract import AdapterError
from apps.production_apply_launcher import execute
from apps.sheet_vitrina_v1_promo_live_source_smoke import _write_promo_run_fixture
from packages.application.promo_campaign_archive import sync_promo_campaign_archive
from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
from packages.application.inventory_retention import InventoryRetentionError, prove_inventory_retention
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.web_vitrina_compact_table import CELL_FIELDS

DAY = '2026-10-07'


class PromoInventoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.globals = patch.multiple(inventory_fixture, DAY=DAY, CAPTURED=DAY+'T18:23:11Z', OBSERVED=DAY+'T18:16:00Z')
        self.globals.start(); self.addCleanup(self.globals.stop)
        self.fixture = inventory_fixture.ManagementInventoryTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        bundle=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
        bundle['bundle_version']+='__94_sku_retention'
        bundle['config_v2'] += [{'nm_id':2000000000+i,'enabled':True,'display_name':f'Fixture {i}',
            'group':'Other','display_order':34+i} for i in range(94-len(bundle['config_v2']))]
        self.assertEqual(self.runtime.ingest_bundle(bundle,activated_at='2026-09-07T10:01:00Z').status,'accepted')
        self.fixture.state=self.runtime.load_current_state()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version<>?",(self.fixture.state.bundle_version,))
        ids = [c.nm_id for c in self.fixture.state.config_v2 if c.enabled]
        self.fixture.prepare_quantities(ids)
        plan = self.fixture.plan
        data, status = plan.sheets[:2]
        promo = [[metric, f'SKU:{nm}|{metric}', ''] for nm in ids for metric in SKU_METRICS]
        promo += [[key, key, ''] for key in TOTAL_METRICS.values()]
        data = replace(data, rows=data.rows+promo, row_count=len(data.rows)+len(promo), write_rect=f'A1:C{len(data.rows)+len(promo)+1}')
        status = replace(status, rows=[['promo_by_price[today_current]', 'incomplete', DAY, '', '', '', '', len(ids), 0, '', 'old']], row_count=1, write_rect='A1:K2')
        self.fixture.plan = replace(plan, sheets=[data, status], metadata={**plan.metadata,
            'refresh_diagnostics': {'source_slots': [{'source_key':'promo_by_price','slot_kind':'today_current',
                'requested_date':DAY,'status':'incomplete','rows_accepted':0}],
                'source_summary':[{'source_key':'promo_by_price','status_counts':{'incomplete':1}}]}})
        self.fixture.book["fixture_revision"] = "promo-ready"
        self.fixture.seed(self.fixture.book)
        _write_promo_run_fixture(runtime_dir=self.runtime.runtime_dir, run_name=DAY+'__fixture',
            promo_folder='2400__2300__promo', promo_id=2400, period_id=2300, promo_title='Promo',
            promo_period_text='07 октября 02:00 -> 07 октября 23:59', promo_start_at=DAY+'T02:00', promo_end_at=DAY+'T23:59',
            workbook_rows=[{'nm_id':nm,'plan_price':508.0} for nm in ids[:7]])
        sync_promo_campaign_archive(self.runtime.runtime_dir)
        from types import SimpleNamespace
        self.runtime.save_temporal_source_slot_snapshot(source_key='prices_snapshot', snapshot_date=DAY,
            snapshot_role='accepted_current_snapshot', captured_at=DAY+'T18:00:00Z',
            payload=SimpleNamespace(kind='success', snapshot_date=DAY, items=[SimpleNamespace(nm_id=nm, price_seller=508., price_seller_discounted=508.) for nm in ids]))
        from packages.application.calculation_parameters_v4 import ensure_proxy_v4_schema
        from packages.application.calculation_parameters import ensure_calculation_parameters_schema
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            ensure_proxy_v4_schema(conn)
            ensure_calculation_parameters_schema(conn)
        self.adapter = PromoArchivePublicationAdapter()
        self.request = {'runtime_dir':str(self.runtime.runtime_dir),'dates':[DAY]}

    def ready(self):
        with closing(_connect(self.runtime.db_path, readonly=True)) as conn:
            return dict(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(self.fixture.state.bundle_version,DAY)).fetchone())

    def native(self):
        with window_read_context(self.runtime.db_path, runtime_dir=self.runtime.runtime_dir):
            dated = NativeDatedCompiler(self.runtime, datetime.fromisoformat('2026-10-08T12:00:00+05:00'), DAY, DAY, group_blocks=True).compile(DAY)
        return {key:dict(zip(CELL_FIELDS,value)) for key,value in dated['cells'].items()}

    def accepted(self):
        plan = _deserialize_sheet_vitrina_plan(self.ready()['plan_json'])
        return self.fixture.read(current='2026-10-08', plan=plan)['dates'].get(DAY)

    def apply(self, operation='promo-owner'):
        preview = self.adapter.preview(self.request, operation)
        self.adapter.apply(self.request, operation, preview)
        self.assertEqual(self.adapter.readback(self.request, operation)['state'],'applied')
        return preview

    def test_atomic_publication_chain_native_and_rollback(self):
        old = self.accepted(); before_native=self.native()
        self.assertTrue(old['scopes']['TOTAL']['accepted_preliminary'])
        preview = self.apply()
        after = self.accepted()
        self.assertEqual(after['captured_at'], old['captured_at'])
        self.assertEqual(after['scopes']['TOTAL']['total'],old['scopes']['TOTAL']['total'])
        after_native=self.native()
        inventory_keys={key for key in before_native if key.split('|')[-1].removeprefix('total_')=='stock_total' or key.split('|')[-1].removeprefix('total_').startswith('inventory_')}
        self.assertTrue(inventory_keys)
        for key in inventory_keys:
            self.assertEqual(after_native[key], before_native[key], key)
            if after_native[key]['value'] is not None:
                self.assertIn('Предварительно',after_native[key]['quality_label'],key)
                self.assertFalse(after_native[key]['inventory_finalization_digest'],key)
        self.assertEqual(after_native[f'SKU:{self.fixture.nms[0]}|promo_count_by_price']['value'],1.0)
        self.assertTrue(preview['scope']['inventory_retention'][DAY]['dates'][DAY])
        original=after['accepted_publication']['publication_operation_id']
        self.apply('promo-owner-2')
        self.assertEqual(self.accepted()['accepted_publication']['publication_operation_id'],original)
        inverse=rollback_preview(self.runtime.runtime_dir,'promo-owner-2')
        rollback_apply(self.runtime.runtime_dir,'promo-owner-2',inverse['after_target_sha256'])
        self.assertIsNotNone(self.accepted())
        # A later unrelated value is retained; backup inventory remains exactly
        # certified, and the restored bytes need their own retention journal.
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            current=json.loads(self.ready()['plan_json'])
            next(row for row in current['sheets'][0]['rows'] if row[1].endswith('|our_wb_unit_cost_rub'))[2]=778
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(json.dumps(current),DAY))
        inverse=rollback_preview(self.runtime.runtime_dir,'promo-owner')
        rollback_apply(self.runtime.runtime_dir,'promo-owner',inverse['after_target_sha256'])
        self.assertIsNotNone(self.accepted())
        restored=json.loads(self.ready()['plan_json'])
        self.assertEqual(next(row for row in restored['sheets'][0]['rows'] if row[1].endswith('|our_wb_unit_cost_rub'))[2],778)
        with closing(_connect(self.runtime.db_path,readonly=True)) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sheet_vitrina_v1_ready_publications WHERE operation_id='promo-owner:rollback:inventory_retention:2026-10-07'").fetchone())

    def test_stale_full_missing_scope_is_replaced_and_cas_covers_quality(self):
        from packages.application.ready_publication import digest
        from apps.promo_archive_publication import _update_plan
        scopes=[f'SKU:{nm}' for nm in self.fixture.nms]
        before=self.ready();raw=json.loads(before['plan_json'])
        cells=raw['metadata'].setdefault('server_cell_presentation',{})
        for metric in SKU_METRICS:
            cells[TOTAL_METRICS[metric]]={DAY:{'quality_state':'partial','quality_reason':'stale missing evidence',
                'completeness_state':'partial','missing_sku_count':94,
                'metric_scope_evidence':{'operand_date':DAY,'applicable_scope':scopes,
                    'sku_metric_keys':[metric],'missing_scope':scopes,'partial_scope':[],'group_scopes':{}}}}
            for nm in self.fixture.nms:
                cells[f'SKU:{nm}|{metric}']={DAY:{'quality_state':'partial','reason':'stale observation'}}
        original_raw=json.dumps(raw,ensure_ascii=False,separators=(',',':'))
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            # This disposable fixture's initial READY owner accepted its original
            # inventory and a missing promo source in this same old publication.
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(original_raw,DAY))
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET after_digest=? WHERE after_digest=?',(digest(original_raw),digest(before['plan_json'])))
        old_native=self.native();stock_before=self.accepted()
        self.assertEqual(old_native['TOTAL|total_promo_participation']['missing_sku_count'],94)
        self.assertEqual(old_native['TOTAL|total_promo_participation']['completeness_state'],'partial')
        self.apply('complete-roster')
        complete_row=self.ready();complete=json.loads(complete_row['plan_json']);native=self.native()
        for metric in SKU_METRICS:
            key=TOTAL_METRICS[metric];cell=native[key]
            self.assertEqual(cell['completeness_state'],'complete',key)
            self.assertEqual(cell['missing_sku_count'],0,key)
            self.assertNotIn('SKU с отсутствующей',cell['quality_reason'],key)
            self.assertNotIn('stale',cell['quality_reason'],key)
            evidence=complete['metadata']['server_cell_presentation'][key][DAY]['metric_scope_evidence']
            self.assertEqual(set(evidence['applicable_scope']),set(scopes));self.assertEqual(evidence['missing_scope'],[])
            self.assertEqual(evidence['partial_scope'],[])
        self.assertEqual(native['TOTAL|total_promo_participation']['value'],7.0)
        self.assertEqual(self.accepted()['scopes']['TOTAL']['total'],stock_before['scopes']['TOTAL']['total'])
        # Partial input never promotes numeric remnants to proven completeness.
        with closing(_connect(self.runtime.db_path,readonly=True)) as conn:
            source=json.loads(conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='promo_by_price' AND snapshot_date=?",(DAY,)).fetchone()[0])
        partial=deepcopy(source);partial['kind']='incomplete';partial['covered_count']=93;partial['items']=partial['items'][:-1]
        unchanged=deepcopy(raw)
        with self.assertRaisesRegex(AdapterError,'completeness-unproven'):
            _update_plan(unchanged,{DAY:partial},{DAY})
        self.assertEqual(unchanged,raw)
        for invalid in (float('nan'),float('inf'),float('-inf'),None):
            bad=deepcopy(source);bad['items'][0]['promo_count_by_price']=invalid
            unchanged=deepcopy(raw)
            with self.assertRaisesRegex(AdapterError,'completeness-value-invalid'):
                _update_plan(unchanged,{DAY:bad},{DAY})
            self.assertEqual(unchanged,raw)
        # Completeness of these composite operands does not establish end-of-day
        # freshness: keep the source-owned warning and marker on SKU and TOTAL.
        composite=deepcopy(source);composite['observation_quality']='historical_composite_observation_only'
        composite['diagnostics']['historical_reconstruction']={'identity_observed_at':DAY+'T18:19:00Z','price_observed_at':DAY+'T18:12:00Z'}
        observed=deepcopy(raw);_update_plan(observed,{DAY:composite},{DAY})
        for key in [*TOTAL_METRICS.values(),f'SKU:{self.fixture.nms[0]}|promo_participation']:
            cell=observed['metadata']['server_cell_presentation'][key][DAY]
            self.assertEqual(cell['completeness_state'],'complete');self.assertEqual(cell['missing_sku_count'],0)
            self.assertEqual(cell['quality_state'],'preliminary');self.assertEqual(cell['state'],'unconfirmed')
            self.assertIn('Полнота на конец дня не подтверждена',cell['quality_reason'])
        # A metadata-only drift must invalidate both readback and inverse CAS.
        tampered=deepcopy(complete)
        tampered['metadata']['server_cell_presentation'][TOTAL_METRICS['promo_participation']][DAY]['missing_sku_count']=1
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(json.dumps(tampered),DAY))
        self.assertEqual(self.adapter.readback(self.request,'complete-roster')['state'],'ambiguous')
        with self.assertRaisesRegex(AdapterError,'after-target-drift'):
            rollback_preview(self.runtime.runtime_dir,'complete-roster')
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(complete_row['plan_json'],DAY))
        inverse=rollback_preview(self.runtime.runtime_dir,'complete-roster')
        rollback_apply(self.runtime.runtime_dir,'complete-roster',inverse['after_target_sha256'])
        self.assertEqual(self.ready()['plan_json'],original_raw)
        restored=self.native()
        self.assertEqual(restored['TOTAL|total_promo_participation']['missing_sku_count'],94)
        self.assertIsNotNone(self.accepted())

    def test_legacy_projection_receipt_and_repair_remain_verifiable(self):
        import apps.promo_archive_publication as owner
        candidate_factory=owner._candidate;update=owner._update_plan
        def legacy_update(plan,results,dates):
            return update(plan,results,dates,update_completeness=False)
        def legacy_candidate(*args,**kwargs):
            with patch.object(owner,'_update_plan',side_effect=legacy_update):
                candidate=candidate_factory(*args,**kwargs)
            candidate.pop('presentation_contract')
            candidate['expected_target_sha']=owner._digest(owner._expected_target_image(candidate))
            return candidate
        # Exercise the previous transform and projection, then use only the new
        # reader/repair/rollback code against that retained unversioned journal.
        with patch.object(owner,'_candidate',side_effect=legacy_candidate), patch.object(owner,'publish_inventory_retention',return_value=None):
            preview=self.adapter.preview(self.request,'legacy-projection')
            self.adapter.apply(self.request,'legacy-projection',preview)
        self.assertFalse(preview['scope'].get('presentation_contract'))
        self.assertEqual(self.adapter.readback(self.request,'legacy-projection')['state'],'applied')
        self.assertIsNone(self.accepted())
        request={'runtime_dir':str(self.runtime.runtime_dir),'mode':'repair_inventory_retention','publication_operation_id':'legacy-projection'}
        repair=self.adapter.preview(request,'legacy-projection-retention')
        self.adapter.apply(request,'legacy-projection-retention',repair)
        self.assertEqual(self.adapter.readback(request,'legacy-projection-retention')['state'],'applied')
        self.assertIsNotNone(self.accepted())
        inverse=rollback_preview(self.runtime.runtime_dir,'legacy-projection')
        rollback_apply(self.runtime.runtime_dir,'legacy-projection',inverse['after_target_sha256'])
        self.assertIsNotNone(self.accepted())

    def test_uncertified_absence_and_unrelated_capture_stay_absent(self):
        from packages.application.sheet_vitrina_v1_inventory_history import CAPTURES_TABLE
        # All earlier captures at this date are intentionally retained. The
        # exact new book binding has never been accepted/captured.
        from packages.application import fbs_accounting_runtime as accounting
        book=deepcopy(self.fixture.book);book['fixture_revision']='uncertified'
        previous=accounting.load(self.runtime.runtime_dir)[1]
        version=accounting._save_book(self.runtime.runtime_dir,book,expected=previous,operation_id='uncertified-book')
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            raw=json.loads(self.ready()['plan_json'])
            raw['metadata']['fbs_accounting_bindings'][DAY]['book_version']=version
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(json.dumps(raw),DAY))
            captures=conn.execute(f'SELECT COUNT(*) FROM {CAPTURES_TABLE}').fetchone()[0]
        self.assertGreater(captures,0)
        preview=self.apply('uncertified-absence')
        self.assertEqual(preview['scope']['inventory_retention'][DAY]['dates'],{})
        self.assertIsNone(self.accepted())
        with closing(_connect(self.runtime.db_path,readonly=True)) as conn:
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {CAPTURES_TABLE}').fetchone()[0],captures)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_publications WHERE kind='inventory_retention'").fetchone()[0],0)

    def test_missing_original_and_inventory_drift_rejected(self):
        before=self.ready(); after=deepcopy(before)
        raw=json.loads(after['plan_json']); raw['sheets'][0]['rows'][0][2]+=1; after['plan_json']=json.dumps(raw)
        with closing(_connect(self.runtime.db_path, readonly=True)) as conn:
            with self.assertRaisesRegex(InventoryRetentionError,'quantity-cells-changed'):
                prove_inventory_retention(conn,runtime_dir=self.runtime.runtime_dir,before=before,after=after)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_ready_publications')
        with self.assertRaisesRegex(InventoryRetentionError,'original-acceptance-missing'):
            self.adapter.preview(self.request,'missing-original')
        self.assertEqual(self.ready(),before)

    def test_atomic_failure_rolls_back_ready_source_and_receipt(self):
        before=self.ready()
        preview=self.adapter.preview(self.request,'atomic-failure')
        with patch('apps.promo_archive_publication.retention_receipts_match',return_value=False):
            with self.assertRaisesRegex(AdapterError,'receipt-poststate-mismatch'):
                self.adapter.apply(self.request,'atomic-failure',preview)
        self.assertEqual(self.ready(),before)
        with closing(_connect(self.runtime.db_path,readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_publications WHERE kind='inventory_retention'").fetchone()[0],0)
            self.assertIsNone(conn.execute("SELECT 1 FROM temporal_source_snapshots WHERE source_key='promo_by_price'").fetchone())
        self.assertIsNotNone(self.accepted())

    def test_repair_attested_legacy_apply_adds_only_receipt(self):
        before_native=self.native()
        # Simulate the deployed previous owner, which had no retention append.
        with patch('apps.promo_archive_publication.publish_inventory_retention',return_value=None):
            self.apply('legacy-promo-owner')
        self.assertIsNone(self.accepted())
        before=self.ready(); native_missing=self.native()
        request={'runtime_dir':str(self.runtime.runtime_dir),'mode':'repair_inventory_retention','publication_operation_id':'legacy-promo-owner'}
        preview=self.adapter.preview(request,'inventory-repair')
        args=dict(adapter_name='fixture',operation_id='inventory-repair',request=request,adapters={'fixture':self.adapter},
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        self.assertEqual(execute(action='apply',**args)['state'],'applied')
        with patch.object(self.adapter,'apply',side_effect=AssertionError('never a second submit')):
            self.assertEqual(execute(action='apply',**args)['state'],'applied')
        readback=self.adapter.readback(request,'inventory-repair')
        self.assertEqual(readback['state'],'applied'); self.assertTrue(readback['source_ready_book_unchanged'])
        self.assertEqual(self.ready(),before)
        accepted=self.accepted(); self.assertIsNotNone(accepted)
        self.assertEqual(accepted['captured_at'],DAY+'T18:23:11Z')
        native=self.native()
        self.assertEqual(native['TOTAL|total_stock_total']['value'],167189)
        self.assertIn('Предварительно',native['TOTAL|total_stock_total']['quality_label'])
        self.assertFalse(native['TOTAL|total_stock_total']['inventory_finalization_digest'])
        self.assertNotEqual(native_missing['TOTAL|total_stock_total'],native['TOTAL|total_stock_total'])
        for key in before_native:
            if key.split('|')[-1].removeprefix('total_')=='stock_total' or key.split('|')[-1].removeprefix('total_').startswith('inventory_'):
                self.assertEqual(native[key],before_native[key],key)
        self.assertEqual(self.adapter.preview(request,'inventory-repair'),preview)
        # Post-owner nonpromo/whole READY drift is never repaired by certification.
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            raw=json.loads(before['plan_json']);raw['sheets'][0]['rows'][0][2]+=1
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(json.dumps(raw),DAY))
        with self.assertRaisesRegex(AdapterError,'full-ready-drift'):
            self.adapter.preview(request,'inventory-repair-drift')

    def test_additive_repair_failure_is_atomic(self):
        with patch('apps.promo_archive_publication.publish_inventory_retention',return_value=None):
            self.apply('legacy-promo-owner')
        before=self.ready()
        request={'runtime_dir':str(self.runtime.runtime_dir),'mode':'repair_inventory_retention','publication_operation_id':'legacy-promo-owner'}
        preview=self.adapter.preview(request,'repair-failure')
        with patch('apps.promo_archive_publication.retention_receipts_match',return_value=False):
            with self.assertRaisesRegex(AdapterError,'poststate-drift'):
                self.adapter.apply(request,'repair-failure',preview)
        self.assertEqual(self.ready(),before)
        self.assertEqual(self.adapter.readback(request,'repair-failure')['state'],'not_submitted')
        with closing(_connect(self.runtime.db_path,readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_publications WHERE kind='inventory_retention'").fetchone()[0],0)
        self.assertIsNone(self.accepted())

    def test_completed_receipt_loss_is_ambiguous(self):
        self.apply()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_ready_publications WHERE kind='inventory_retention'")
        self.assertEqual(self.adapter.readback(self.request,'promo-owner')['state'],'ambiguous')
        self.assertIsNone(self.accepted())

    def test_repair_missing_original_and_backup_hash_rejected(self):
        with patch('apps.promo_archive_publication.publish_inventory_retention',return_value=None):
            self.apply('legacy-promo-owner')
        request={'runtime_dir':str(self.runtime.runtime_dir),'mode':'repair_inventory_retention','publication_operation_id':'legacy-promo-owner'}
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_ready_publications')
        with self.assertRaisesRegex(InventoryRetentionError,'original-acceptance-missing'):
            self.adapter.preview(request,'inventory-repair-missing')
        with closing(_connect(self.runtime.db_path,readonly=True)) as conn:
            path=Path(conn.execute(f'SELECT backup_path FROM {LEDGER}').fetchone()[0])
        with path.open('ab') as file:file.write(b'tamper')
        with self.assertRaisesRegex(AdapterError,'backup-drift'):
            self.adapter.preview(request,'inventory-repair-tampered')


if __name__=='__main__':unittest.main()
