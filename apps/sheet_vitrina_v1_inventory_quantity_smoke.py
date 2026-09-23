#!/usr/bin/env python3
"""Typed stock history: exact sources, real backfill, final public consumers."""
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.ready_publication_fixture import save_ready_fixture
from apps.sheet_vitrina_v1_inventory_history_backfill import run_backfill, InventoryHistoryBackfillError
from apps import sheet_vitrina_v1_inventory_history_backfill as backfill
from packages.application import sheet_vitrina_v1_inventory_history as history
from packages.application import fbs_accounting_runtime as accounting
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.inventory_quantity import bound_book_quantities, resolve_plan_quantities, BOOK_SOURCE, OFFICIAL_SOURCE, CONTRACT
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _serialize_sheet_vitrina_plan
from packages.application.sheet_vitrina_v1_inventory_planning import (
    extend_rows_with_inventory_planning, restore_finalized_inventory_history, apply_fbs_unavailable_presentation,
    apply_fbs_last_good_presentation, inventory_planning_facility_metric_key)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget, SheetVitrinaV1TemporalSlot

DAYS = ['2026-09-08', '2026-09-09']
NOW = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
SHA = 'a' * 40


def book_fixture(nms):
    book = {'schema': accounting.SCHEMA, 'active': True, 'effective_date': DAYS[0],
        'presentations': {}, 'wb_days': {}, 'retained_days': {}, 'shared_days': {},
        'state': {'baseline': {}, 'periods': {}, 'observed_documents': {}}}
    for day in DAYS:
        observed = day + 'T18:16:00Z'
        facilities = {fid: {'facility_id': fid, 'facility_name': name, 'mapping_id': 'map-'+fid,
            'stock_run_id': day+'-'+fid, 'stock_digest': 'digest-'+fid, 'captured_at': observed}
            for fid, name in [('moscow','FF Москва'),('orenburg','FF Оренбург')]}
        snapshot = {'date': day, 'id': day+'-official', 'digest': 'official-digest-'+day,
            'source': OFFICIAL_SOURCE, 'complete': True, 'captured_at': observed, 'facility_evidence': facilities,
            'rows': [{'nm_id': nm, 'facility_id': fid, 'quantity': (3 if fid == 'moscow' else 2) if nm == nms[0] else 0}
                     for nm in nms for fid in facilities]}
        wb = {'business_date': day, 'contract': 'shared_sku_cost_wb_source_v1', 'version_id': day+'-wb',
            'authority_complete': True, 'requested_nm_ids': nms, 'complete': True,
            'source': {'snapshot_date': day, 'fetched_at': day+'T18:19:00Z', 'snapshot_id': day+'-wb-snapshot'},
            'rows': [{'nm_id': nm, 'quantity': 12 if nm == nms[0] else 0,
                      'components': {'physical': 8 if nm == nms[0] else 0, 'to_customer': 4 if nm == nms[0] else 0}}
                     for nm in nms]}
        payload = {'source': BOOK_SOURCE, 'date': day, 'quality': 'preliminary', 'quantity_snapshot': snapshot,
            'rows': {str(nm): {'nm_id': nm, 'wb_physical': 8 if nm == nms[0] else 0,
                'stock_total': 13 if nm == nms[0] else 0,
                'identity': {'name': 'Retained hidden' if i >= 71 else 'Товар', 'hidden': i >= 71}}
                for i, nm in enumerate(nms)}, 'totals': {'wb_physical': 8, 'stock_total': 13}}
        payload['version_id'] = fingerprint(payload)
        book['presentations'][day] = payload
        book['wb_days'][day] = wb
        book['state']['periods'][day] = {'status': 'closed', 'snapshot': deepcopy(snapshot)}
    return book


def binding(book, day, target):
    return {'book_version': fingerprint(book), 'presentation_version': book['presentations'][day]['version_id'],
        'date': day, 'effective_date': DAYS[0], 'source': BOOK_SOURCE, 'quality': book['presentations'][day]['quality'],
        'ready_target': target}


def plan_fixture(nms, current, closed=None):
    dates = ([closed] if closed else []) + [current]
    rows = [['Stock', 'TOTAL|total_stock_total', *[13 for d in dates]]]
    rows += [['Stock', f'SKU:{nm}|stock_total', *[13 if nm == nms[0] else 0 for d in dates]] for nm in nms]
    rows += [['Money', f'SKU:{nms[0]}|our_wb_unit_cost_rub', *[777 for d in dates]]]
    return SheetVitrinaV1Envelope('fixture', 'fixture-'+current, closed or current, dates,
        ([SheetVitrinaV1TemporalSlot('yesterday_closed','Closed',closed)] if closed else [])+
        [SheetVitrinaV1TemporalSlot('today_current','Current',current)], {},
        [SheetVitrinaWriteTarget('DATA_VITRINA','A1',f'A1:{chr(66+len(dates))}{len(rows)+1}','A:D','values',False,
                                ['label','key',*dates], rows,len(rows),2+len(dates)),
         SheetVitrinaWriteTarget('STATUS','A1','A1:K1','A:K','values',False,
             ['source_key','kind','freshness','snapshot_date','date','date_from','date_to','requested_count','covered_count','missing_nm_ids','note'],[],0,11)],
        metadata={'server_cell_presentation':{row[1]:{d:{'source':'stocks'} for d in dates} for row in rows if row[1].endswith('stock_total')}})


def held_fixture(runtime_dir, window='stock-fixture-hold'):
    from packages.application import business_data_write_barrier as barrier
    policy = {'master_desired':False, 'revision':1, 'policy_fingerprint':'fixture-policy'}
    hold = {'schema_version':'business_data_maintenance_v1', 'phase':'held',
            'held_at':'2026-09-11T09:00:00Z', 'hold_readback':{'quiet':True, 'auto_updates':policy}}
    for name, value in [('.business-data-maintenance.json',hold),('.auto-updates-policy.json',policy)]:
        path=runtime_dir/name;path.write_text(json.dumps(value));path.chmod(0o600)
    barrier.acquire_barrier(runtime_dir,window_id=window,window_kind='snapshot',plan_fingerprint=fingerprint({'fixture':1}),
                            approval_reference='local-fixture',actor='fixture',reason='local fixture')
    barrier.confirm_barrier_hold(runtime_dir,window_id=window,plan_fingerprint=fingerprint({'fixture':1}),maintenance_state=hold)
    return window


@contextmanager
def prepared_backfill_fixture():
    with TemporaryDirectory(prefix='stock-review-regression-') as tmp:
        root=Path(tmp);runtime=RegistryUploadDbBackedRuntime(runtime_dir=root/'runtime')
        bundle=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
        runtime.ingest_bundle(bundle,activated_at='2026-09-07T10:00:00Z');state=runtime.load_current_state()
        nms=[int(c.nm_id) for c in state.config_v2 if c.enabled][:2]
        for current,closed in [(DAYS[0],None),(DAYS[1],DAYS[0]),('2026-09-10',DAYS[1])]:
            save_ready_fixture(runtime,current_state=state,refreshed_at=current+'T18:30:00Z',plan=plan_fixture(nms,current,closed))
        book=book_fixture(nms);accounting._save_book(runtime.runtime_dir,book,expected=None,operation_id='review-fixture')
        sources=[]
        with closing(sqlite3.connect(runtime.db_path)) as conn,conn:
            for day in DAYS:
                payload=json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(state.bundle_version,day)).fetchone()[0])
                target={'bundle_version':state.bundle_version,'as_of_date':day};bound=binding(book,day,target)
                payload.setdefault('metadata',{})['fbs_accounting_bindings']={day:bound}
                payload['metadata']['ready_publication_target']=target
                conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(json.dumps(payload),state.bundle_version,day))
                sources.append({'business_date':day,'ready_target':target,'binding':bound})
        source_file=root/'sources.json';source_file.write_text(json.dumps({'sources':sources}))
        sha=root/'sha';sha.write_text(SHA)
        kwargs=dict(runtime_dir=runtime.runtime_dir,evidence_dir=root/'evidence',deployed_sha=SHA,deployed_sha_file=sha,
                    now=NOW,maintenance_window_id=held_fixture(runtime.runtime_dir))
        with patch.object(backfill,'_stock_quiet_os_readback',return_value={'fixture':'quiet OS inventory'}):
            yield runtime,kwargs,source_file,nms


class TypedInventoryTests(unittest.TestCase):
    def operands(self, book=None, day=DAYS[0], nms=None):
        book = book or book_fixture(nms or [1,2])
        target = {'bundle_version': 'b', 'as_of_date': day}
        return bound_book_quantities(book=book, book_version=fingerprint(book), binding=binding(book,day,target),
                                     target=target, day=day, require_closed=True)

    def test_physical_available_scope_and_quality(self):
        value = self.operands(nms=list(range(1,93)))
        self.assertEqual(len(value['components']),279)
        rows = { (c['scope_key'],c['component_id']): c for c in value['components'] }
        self.assertEqual([rows[('TOTAL',fid)]['quantity'] for fid in ('WB','moscow','orenburg')],[8,3,2])
        self.assertEqual(rows[('SKU:92','moscow')]['state'],'exact_zero')
        self.assertTrue(rows[('SKU:92','moscow')]['provenance']['zero_evidence'])
        self.assertIsNone(rows[('SKU:1','moscow')]['provenance']['physical'])
        self.assertIsNone(rows[('SKU:1','moscow')]['provenance']['reserved'])
        self.assertEqual(rows[('SKU:1','WB')]['provenance']['cost_quality'],'preliminary')

    def test_missing_duplicate_midnight_binding_and_no_rounding(self):
        mutations = [
            (lambda b: b['presentations'][DAYS[0]]['quantity_snapshot']['rows'].append(
                deepcopy(b['presentations'][DAYS[0]]['quantity_snapshot']['rows'][0])), 'duplicate'),
            (lambda b: b['presentations'][DAYS[0]]['quantity_snapshot'].update(captured_at=DAYS[0]+'T19:00:00Z'), 'source_date'),
            (lambda b: b['presentations'][DAYS[0]]['quantity_snapshot']['rows'][0].update(quantity=1.2), 'invalid_inventory'),
        ]
        for mutate, reason in mutations:
            with self.subTest(reason=reason):
                book=book_fixture([1,2]);mutate(book)
                book['state']['periods'][DAYS[0]]['snapshot']=deepcopy(book['presentations'][DAYS[0]]['quantity_snapshot'])
                with self.assertRaisesRegex(ValueError,reason): self.operands(book)
        book=book_fixture([1,2]); book['presentations'][DAYS[0]]['quantity_snapshot']['rows'].pop()
        book['state']['periods'][DAYS[0]]['snapshot']=deepcopy(book['presentations'][DAYS[0]]['quantity_snapshot'])
        self.assertTrue(all(c['state']=='missing' for c in self.operands(book)['components'] if c['component_id']!='WB'))
        plan=plan_fixture([1,2],DAYS[0]); target={'bundle_version':'b','as_of_date':plan.as_of_date}
        good=book_fixture([1,2]); plan=replace(plan, metadata={'fbs_accounting_bindings':{DAYS[0]:binding(good,DAYS[0],target)}})
        with self.assertRaisesRegex(ValueError,'bound_book_required'): resolve_plan_quantities(plan,day=DAYS[0])
        unknown=replace(plan,metadata={'server_cell_presentation':{'TOTAL|total_stock_total':{DAYS[0]:{'source':'unknown'}}}})
        self.assertIsNone(history._ready_wb_components(unknown,business_date=DAYS[0])['TOTAL'])

    def test_current_and_closure_producers_and_immutable_finalized_days(self):
        book=book_fixture([1,2]);plan=plan_fixture([1,2],DAYS[1],DAYS[0]);target={'bundle_version':'b','as_of_date':plan.as_of_date}
        plan=replace(plan,metadata={'fbs_accounting_bindings':{d:binding(book,d,target) for d in DAYS}, 'ready_publication_target':target})
        with closing(sqlite3.connect(':memory:')) as conn:
            history.ensure_inventory_history_schema(conn)
            prepared=history.prepare_inventory_history_from_ready_plan(conn,plan=plan,bundle_version='b',refreshed_at=DAYS[1]+'T18:30:00Z',prepared_book=book)
            for role in ('current','closed'):
                wb=next(c for c in prepared[role]['components'] if c['scope_key']=='TOTAL' and c['component_id']=='WB')
                self.assertEqual(wb['quantity'],8)
            history.capture_inventory_history_from_ready_plan(conn,plan=plan,bundle_version='b',refreshed_at=DAYS[1]+'T18:30:00Z',prepared=prepared)
            again=history.prepare_inventory_history_from_ready_plan(conn,plan=plan,bundle_version='b',refreshed_at=DAYS[1]+'T18:40:00Z',prepared_book=book)
            self.assertEqual(again['closed_date'],'')
            self.assertIsNone(again['closed'])

    def test_epoch_facility_mapping_and_cost_independence(self):
        from packages.application.sheet_vitrina_v1_inventory_planning import _historical_metric_value, _public_metric_specs
        book=book_fixture([1,2]); book['presentations'][DAYS[0]]['quality']='cost_incomplete'
        operands=self.operands(book)
        with TemporaryDirectory() as tmp:
            db=Path(tmp)/'history.sqlite3'
            with closing(sqlite3.connect(db)) as conn, conn:
                history.ensure_inventory_history_schema(conn)
                for day,typed in [('2026-09-07',False),(DAYS[0],True)]:
                    components=deepcopy(operands['components'])
                    if not typed:
                        for item in components: item['provenance']={}
                    capture=history.append_inventory_history_capture(conn,business_date=day,capture_kind='historical_backfill',
                        formula_version=CONTRACT if typed else 'inventory_planning_v1',facility_roster=operands['facility_roster'],
                        source_manifest=operands['source_manifest'] if typed else {'contract':'legacy'},components=components,captured_at=day+'T18:30:00Z')
                    history.append_inventory_history_finalization(conn,business_date=day,capture_id=capture['capture_id'],
                        finalization_identity='fixture:'+day,finalized_at=day+'T18:30:00Z',provenance={})
            data=history.read_inventory_history_window(db,dates=['2026-09-07',DAYS[0]],current_date='',lifecycle_quality_resolver=lambda *a:{})
            planning={'fbs':{'facilities':[{'facility_id':'moscow','name':'Совсем другой склад','active':False}]}}
            spec=next(s for s in _public_metric_specs(planning,history=data) if s.facility_id=='moscow')
            legacy=_historical_metric_value(spec,data['dates']['2026-09-07']['scopes']['SKU:1'])
            official=_historical_metric_value(spec,data['dates'][DAYS[0]]['scopes']['SKU:1'])
            self.assertNotIn('semantic_kind',legacy[1])
            self.assertEqual(official[0],3)
            self.assertEqual(official[1]['semantic_kind'],'fbs_available_qty')
            self.assertIn('FF Москва',official[1]['quality_reason'])
            self.assertNotIn('Совсем другой',official[1]['quality_reason'])
            self.assertEqual(official[1]['quantity_sources'][0]['provenance']['cost_quality'],'cost_incomplete')
        self.assertEqual(history._optional_integer(2**53+1),2**53+1)

    def test_real_backfill_final_reader_and_rollback(self):
        with TemporaryDirectory(prefix='typed-stock-') as tmp:
            root=Path(tmp); runtime=RegistryUploadDbBackedRuntime(runtime_dir=root/'runtime')
            bundle=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
            runtime.ingest_bundle(bundle,activated_at='2026-09-07T10:00:00Z');state=runtime.load_current_state()
            manual=[int(c.nm_id) for c in state.config_v2 if c.enabled]
            nms=sorted(set(manual+list(range(900001,900001+92-len(manual)))))
            self.assertEqual(len(nms),92)
            for current,closed in [(DAYS[0],None),(DAYS[1],DAYS[0]),('2026-09-10',DAYS[1])]:
                save_ready_fixture(runtime,current_state=state,refreshed_at=current+'T18:30:00Z',plan=plan_fixture(nms,current,closed))
            book=book_fixture(nms)
            version=accounting._save_book(runtime.runtime_dir,book,expected=None,operation_id='fixture-book')
            sources=[]
            with sqlite3.connect(runtime.db_path) as conn:
                conn.row_factory=sqlite3.Row
                for day in DAYS:
                    row=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(state.bundle_version,day)).fetchone()
                    plan=json.loads(row[0]);target={'bundle_version':state.bundle_version,'as_of_date':day}
                    bound=binding(book,day,target);plan.setdefault('metadata',{})['fbs_accounting_bindings']={day:bound}
                    plan['metadata']['ready_publication_target']=target
                    conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(json.dumps(plan),state.bundle_version,day))
                    sources.append({'business_date':day,'ready_target':target,'binding':bound})
                before={day:dict(conn.execute(f'SELECT c.capture_id,c.source_digest,f.finalization_digest FROM {history.CAPTURES_TABLE} c JOIN {history.FINALIZATIONS_TABLE} f USING(capture_id) WHERE f.business_date=? ORDER BY f.finalization_sequence DESC LIMIT 1',(day,)).fetchone()) for day in DAYS}
                ready_before=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date').fetchall()
            bad={(day,r['capture_id'],r['source_digest']) for day,r in before.items()}
            block = SheetVitrinaV1WebVitrinaBlock(runtime=runtime,now_factory=lambda:NOW)
            def contract():
                return block.build(
                    page_route='/sheet-vitrina-v1/vitrina',read_route='/v1/sheet-vitrina-v1/web-vitrina',date_from=DAYS[0],date_to=DAYS[1])
            sha=root/'sha';sha.write_text(SHA); source_file=root/'sources.json';source_file.write_text(json.dumps({'sources':sources}))
            kwargs=dict(runtime_dir=runtime.runtime_dir,evidence_dir=root/'evidence',deployed_sha=SHA,deployed_sha_file=sha,now=NOW,maintenance_window_id=held_fixture(runtime.runtime_dir))
            with patch.object(history,'KNOWN_BAD_CAPTURES',bad), patch.object(backfill,'_stock_quiet_os_readback',return_value={'fixture':'quiet OS inventory'}):
                old=contract(); oldrows={r.row_id:r for r in old.rows}
                self.assertEqual(oldrows['TOTAL|total_inventory_wb_total_qty_v1'].values_by_date[DAYS[0]],'')
                self.assertEqual(oldrows['TOTAL|total_inventory_wb_total_qty_v1'].presentation_by_date[DAYS[0]]['quality_state'],'historical_repair_required')
                dry=run_backfill(**kwargs,apply=False,date_from=DAYS[0],date_to=DAYS[1],bound_sources_path=source_file)
                self.assertEqual(dry['status'],'ready');self.assertEqual(dry['target_component_count'],558)
                args=dict(kwargs,manifest_path=Path(dry['manifest_path']),expected_manifest_sha256=dry['manifest_sha256'])
                with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                    original=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?',(DAYS[0],)).fetchone()[0]
                    drift=json.loads(original);drift['metadata']['fbs_accounting_bindings'][DAYS[0]]['presentation_version']='wrong'
                    conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(json.dumps(drift),DAYS[0]))
                with self.assertRaisesRegex(InventoryHistoryBackfillError,'binding changed'):
                    run_backfill(**args,apply=True,approval_reference='local rejected drift')
                with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                    conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(original,DAYS[0]))
                writes=[]
                real_connect=sqlite3.connect
                def traced_connect(*a,**kw):
                    conn=real_connect(*a,**kw)
                    def authorizer(action,table,col,db,trigger):
                        if action in {sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE}:
                            writes.append((table,trigger))
                        return sqlite3.SQLITE_OK
                    conn.set_authorizer(authorizer)
                    return conn
                with patch.object(sqlite3,'connect',side_effect=traced_connect), \
                     patch.object(accounting,'_save_book',side_effect=AssertionError('history repair cannot publish a book')), \
                     patch.object(runtime,'save_sheet_vitrina_ready_snapshot',side_effect=AssertionError('history repair cannot publish ready')):
                    result=run_backfill(**args,apply=True,approval_reference='local fixture')
                self.assertEqual(result['status'],'reconciled')
                self.assertEqual({table for table,trigger in writes}, {
                    history.CAPTURES_TABLE,history.COMPONENTS_TABLE,history.FINALIZATIONS_TABLE,history.APPLIES_TABLE,
                    'sheet_vitrina_v1_ready_input_revisions'})
                self.assertEqual(run_backfill(**args,apply=False,readback=True)['status'],'reconciled')
                self.assertEqual(run_backfill(**args,apply=True,approval_reference='local fixture')['status'],'already_applied')
                writes.clear()
                with patch.object(sqlite3,'connect',side_effect=traced_connect):
                    current=contract()
                    warm=contract()
                self.assertEqual(writes, [], "read-time consumers must not write")
                self.assertEqual(current.rows,warm.rows)
                rows={r.row_id:r for r in current.rows}
                quantity_keys={'stock_total','inventory_wb_total_qty_v1',inventory_planning_facility_metric_key('moscow'),inventory_planning_facility_metric_key('orenburg')}
                def non_target_rows(value):
                    return {r.row_id:(r.values_by_date,r.presentation_by_date) for r in value.rows
                            if r.metric_key.removeprefix('total_') not in quantity_keys}
                self.assertEqual(non_target_rows(old),non_target_rows(current))
                self.assertNotEqual(old.meta.inventory_history_version,current.meta.inventory_history_version)
                for day in DAYS:
                    self.assertEqual(rows['TOTAL|total_inventory_wb_total_qty_v1'].values_by_date[day],8)
                    self.assertEqual(rows['TOTAL|total_stock_total'].values_by_date[day],13)
                    for nm in nms:
                        for fid in ('moscow','orenburg'):
                            key=f'SKU:{nm}|'+inventory_planning_facility_metric_key(fid)
                            self.assertEqual(rows[key].values_by_date[day], (3 if fid=='moscow' else 2) if nm==nms[0] else 0)
                            self.assertEqual(rows[key].presentation_by_date[day]['semantic_kind'],'fbs_available_qty')
                view=build_web_vitrina_view_model(current);adapter=build_web_vitrina_gravity_table_adapter(view)
                serialized=json.loads(json.dumps(asdict(adapter),ensure_ascii=False))
                cell=next(r for r in serialized['rows'] if r['row_id']==f'SKU:{nms[-1]}|'+inventory_planning_facility_metric_key('moscow'))['values']['date:'+DAYS[0]]
                self.assertEqual(cell['quantity_semantic_kind'],'fbs_available_qty')
                self.assertEqual(cell['quantity_source_observed_at'],DAYS[0]+'T18:16:00Z')
                self.assertTrue(cell['inventory_finalization_digest'])
                if os.environ.get('WBC_TYPED_STOCK_BROWSER') == '1':
                    self.browser_check(block, current.meta.inventory_history_version)
                self.assertFalse(current.capabilities.exportable)
                unavailable=apply_fbs_unavailable_presentation(current.rows,reason_ru='legacy unavailable')
                lastgood=apply_fbs_last_good_presentation(unavailable,reason_ru='old lifecycle',last_good_at='2026-09-07T12:00:00Z',source_as_of_date='2026-09-07')
                self.assertEqual(current.rows,lastgood)
                with patch.object(history, 'fbs_lifecycle_quality_coverage', side_effect=AssertionError('typed history must not scan lifecycle')):
                    typed=history.read_inventory_history_window(runtime.db_path,dates=DAYS,current_date='')
                self.assertEqual(typed['dates'][DAYS[0]]['scopes']['TOTAL']['total'],13)
                with sqlite3.connect(runtime.db_path) as conn:
                    self.assertEqual([r[0] for r in ready_before],[r[0] for r in conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date')])
                    revisions_before=dict(conn.execute('SELECT source_table,revision FROM sheet_vitrina_v1_ready_input_revisions'))
                    latest=conn.execute(f'SELECT finalization_digest FROM {history.FINALIZATIONS_TABLE} WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1',(DAYS[0],)).fetchone()[0]
                    with self.assertRaisesRegex(ValueError,'predecessor_changed'):
                        history.append_inventory_history_finalization(conn,business_date=DAYS[0],capture_id=before[DAYS[0]]['capture_id'],finalization_identity='rollback',finalized_at='2026-09-11T10:00:00Z',provenance={},expected_predecessor='wrong')
                    history.append_inventory_history_finalization(conn,business_date=DAYS[0],capture_id=before[DAYS[0]]['capture_id'],finalization_identity='rollback',finalized_at='2026-09-11T10:00:00Z',provenance={},expected_predecessor=latest)
                    revisions_after=dict(conn.execute('SELECT source_table,revision FROM sheet_vitrina_v1_ready_input_revisions'))
                    self.assertEqual({key:revisions_after[key]-value for key,value in revisions_before.items() if revisions_after[key]!=value},
                                     {history.FINALIZATIONS_TABLE:1})
                rolled=contract();self.assertEqual(next(r for r in rolled.rows if r.row_id=='TOTAL|total_inventory_wb_total_qty_v1').values_by_date[DAYS[0]],'')
                self.assertEqual(next(r for r in rolled.rows if r.row_id=='TOTAL|total_stock_total').values_by_date[DAYS[0]],'')
                self.assertEqual(non_target_rows(current),non_target_rows(rolled))
                self.assertEqual(next(r for r in rolled.rows if r.row_id=='TOTAL|total_inventory_wb_total_qty_v1').values_by_date[DAYS[1]],8)
                self.assertNotEqual(rolled.meta.inventory_history_version,current.meta.inventory_history_version)
            self.assertEqual(accounting.load(runtime.runtime_dir)[1],version)

    def test_readback_same_snapshot_and_same_quality_successor(self):
        with prepared_backfill_fixture() as (runtime,kwargs,source_file,nms):
            dry=run_backfill(**kwargs,apply=False,date_from=DAYS[0],date_to=DAYS[1],bound_sources_path=source_file)
            args=dict(kwargs,manifest_path=Path(dry['manifest_path']),expected_manifest_sha256=dry['manifest_sha256'])
            self.assertEqual(run_backfill(**args,apply=True,approval_reference='local fixture')['status'],'reconciled')
            manifest=json.loads(Path(dry['manifest_path']).read_text());item=manifest['captures'][0]
            def successor(tag):
                components=deepcopy(item['components'])
                for row in components:
                    if row['component_kind']=='WB' and row['scope_key'] in ('TOTAL',f'SKU:{nms[0]}'): row['quantity']+=100
                with closing(sqlite3.connect(runtime.db_path)) as writer,writer:
                    captured=history.append_inventory_history_capture(writer,business_date=DAYS[0],capture_kind='historical_backfill',
                        formula_version=item['formula_version'],facility_roster=item['facility_roster'],
                        source_manifest={**item['source_manifest'],'fixture_successor':tag},components=components,captured_at='2026-09-11T10:01:00Z')
                    return history.append_inventory_history_finalization(writer,business_date=DAYS[0],capture_id=captured['capture_id'],
                        finalization_identity=tag,finalized_at='2026-09-11T10:01:00Z',provenance={})
            # Exercise ordinary RO transactional concurrency independently of the
            # operational quiet admission, which deliberately forbids this writer.
            direct=dict(db_path=runtime.db_path,store_registry=backfill.StoreRegistry(runtime.runtime_dir),
                        manifest_path=args['manifest_path'],expected_manifest_sha256=args['expected_manifest_sha256'],
                        deployed_sha=SHA,deployed_sha_file=kwargs['deployed_sha_file'])
            with closing(sqlite3.connect(runtime.db_path)) as keeper:
                self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                keeper.execute(f'SELECT COUNT(*) FROM {history.CAPTURES_TABLE}').fetchone()
                real_reader=backfill.read_inventory_history_window
                def injected_reader(*a,**kw):
                    self.assertTrue(kw['connection'].in_transaction)
                    successor('between-target-and-visible')
                    return real_reader(*a,**kw)
                with patch.object(backfill,'read_inventory_history_window',side_effect=injected_reader):
                    result=backfill._readback_manifest(**direct)
                self.assertEqual(result['status'],'reconciled')
                self.assertEqual(result['current_visible']['dates'][DAYS[0]]['values_by_scope']['TOTAL']['WB']['value'],8)
                result=backfill._readback_manifest(**direct)
                self.assertEqual(result['status'],'superseded')
                self.assertEqual(result['operation_receipt']['status'],'verified')
                self.assertEqual(result['current_visible']['dates'][DAYS[0]]['values_by_scope']['TOTAL']['WB']['value'],108)
                class InjectedConnection:
                    def __init__(self,conn): self.conn=conn;self.did=False
                    def execute(self,sql,*a,**kw):
                        if 'SELECT finalization_sequence,finalization_id' in sql and not self.did:
                            self.did=True;successor('between-target-selects')
                        return self.conn.execute(sql,*a,**kw)
                with backfill._query_only_connection(runtime.db_path) as conn:
                    material=backfill._target_history_state(InjectedConnection(conn),date_from=DAYS[0],date_to=DAYS[1])
                    selected=material['by_date'][DAYS[0]]
                    self.assertTrue(conn.in_transaction)
                    self.assertIn(selected['capture_id'],material['capture_ids'])
                    self.assertEqual(len(selected['components']),9)
                # Legitimate unrelated counters still do not invalidate the retained receipt.
                with closing(sqlite3.connect(runtime.db_path)) as writer,writer:
                    history.append_inventory_history_capture(writer,business_date='2026-09-10',capture_kind='accepted_refresh',
                        formula_version=item['formula_version'],facility_roster=item['facility_roster'],source_manifest={'neighbor':1},
                        components=item['components'],captured_at='2026-09-11T10:02:00Z')
                self.assertEqual(backfill._readback_manifest(**direct)['operation_receipt']['status'],'verified')

    def test_same_manifest_readback_after_reconfirmed_or_new_held_window(self):
        from packages.application import business_data_write_barrier as barrier
        with prepared_backfill_fixture() as (runtime,kwargs,source_file,nms):
            dry=run_backfill(**kwargs,apply=False,date_from=DAYS[0],date_to=DAYS[1],bound_sources_path=source_file)
            args=dict(kwargs,manifest_path=Path(dry['manifest_path']),expected_manifest_sha256=dry['manifest_sha256'])
            self.assertEqual(run_backfill(**args,apply=True,approval_reference='local fixture')['status'],'reconciled')
            original=json.loads(args['manifest_path'].read_text())['read_admission']
            hold=json.loads((runtime.runtime_dir/'.business-data-maintenance.json').read_text())
            state=barrier._load_state(runtime.runtime_dir)
            with patch.object(barrier,'_utc_now',return_value='2026-09-11T11:00:00Z'):
                barrier.confirm_barrier_hold(runtime.runtime_dir,window_id=kwargs['maintenance_window_id'],
                    plan_fingerprint=state['plan_fingerprint'],maintenance_state=hold)
            def verify_readback():
                # All public readback SQLite opens must be query-only and the
                # complete local fixture inventory/bytes must remain unchanged.
                def inventory():
                    return {str(p.relative_to(runtime.runtime_dir.parent)):p.read_bytes()
                            for p in runtime.runtime_dir.parent.rglob('*') if p.is_file()}
                before=inventory();real_connect=sqlite3.connect
                def readonly_connect(*a,**kw):
                    self.assertTrue(kw.get('uri'))
                    self.assertIn('?mode=ro&immutable=1',str(a[0]))
                    conn=real_connect(*a,**kw)
                    def authorizer(action,*rest):
                        self.assertNotIn(action,(sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE))
                        return sqlite3.SQLITE_OK
                    conn.set_authorizer(authorizer)
                    return conn
                with patch.object(sqlite3,'connect',side_effect=readonly_connect):
                    result=run_backfill(**args,apply=False,readback=True)
                self.assertEqual(inventory(),before)
                self.assertEqual(result['status'],'reconciled')
                self.assertEqual(result['operation_receipt']['status'],'verified')
                self.assertEqual(result['current_visible']['status'],'matches_operation')
                self.assertEqual(result['current_visible']['dates'][DAYS[0]]['values_by_scope']['TOTAL']['WB']['value'],8)
                self.assertEqual(result['manifest_sha256'],dry['manifest_sha256'])
                self.assertEqual(result['exact_manifest_apply_receipt_count'],1)
                self.assertTrue(result['query_only'])
                self.assertFalse(result['database_written'])
                self.assertFalse(result['retry_apply_allowed'])
                self.assertEqual(result['original_apply_read_admission'],original)
                self.assertEqual(result['read_admission']['window_id'],args['maintenance_window_id'])
                self.assertNotEqual(result['read_admission']['barrier_fingerprint'],original['barrier_fingerprint'])
                # Drifted apply must fail before reaching the writer/receipt path.
                with patch.object(backfill,'_apply_manifest',side_effect=AssertionError('no apply entry allowed')):
                    with self.assertRaisesRegex(InventoryHistoryBackfillError,'manifest maintenance/control binding changed'):
                        run_backfill(**args,apply=True,approval_reference='local fixture')
                self.assertEqual(inventory(),before)
                return result
            reconfirmed=verify_readback()
            self.assertEqual(reconfirmed['read_admission']['window_id'],original['window_id'])
            barrier.release_barrier(runtime.runtime_dir,window_id=kwargs['maintenance_window_id'],
                plan_fingerprint=state['plan_fingerprint'],actor='fixture',reason='local fixture only',
                restore_readback={'status':'restored','exact_prior_state_restored':True})
            args['maintenance_window_id']=held_fixture(runtime.runtime_dir,window='stock-fixture-readback-hold')
            replacement=verify_readback()
            self.assertNotEqual(replacement['read_admission']['window_id'],original['window_id'])

    def test_committed_readback_pending_recovers_after_hold_reconfirmation(self):
        from packages.application import business_data_write_barrier as barrier
        with prepared_backfill_fixture() as (runtime,kwargs,source_file,nms):
            dry=run_backfill(**kwargs,apply=False,date_from=DAYS[0],date_to=DAYS[1],bound_sources_path=source_file)
            args=dict(kwargs,manifest_path=Path(dry['manifest_path']),expected_manifest_sha256=dry['manifest_sha256'])
            real_apply=backfill._apply_manifest
            def reconfirm_after_commit(**apply_args):
                result=real_apply(**apply_args)
                self.assertEqual(result['status'],'reconciled')
                state=barrier._load_state(runtime.runtime_dir)
                hold=json.loads((runtime.runtime_dir/'.business-data-maintenance.json').read_text())
                with patch.object(barrier,'_utc_now',return_value='2026-09-11T11:01:00Z'):
                    barrier.confirm_barrier_hold(runtime.runtime_dir,window_id=kwargs['maintenance_window_id'],
                        plan_fingerprint=state['plan_fingerprint'],maintenance_state=hold)
                return result
            with patch.object(backfill,'_apply_manifest',side_effect=reconfirm_after_commit):
                pending=run_backfill(**args,apply=True,approval_reference='local fixture')
            self.assertEqual(pending['status'],'committed_readback_pending')
            self.assertTrue(pending['database_written'])
            self.assertFalse(pending['retry_apply_allowed'])
            recovered=run_backfill(**args,apply=False,readback=True)
            self.assertEqual(recovered['status'],'reconciled')
            self.assertEqual(recovered['operation_receipt']['status'],'verified')
            self.assertEqual(recovered['manifest_sha256'],pending['manifest_sha256'])
            self.assertEqual(recovered['exact_manifest_apply_receipt_count'],1)
            self.assertFalse(recovered['retry_apply_allowed'])

    def test_guarded_book_sidecars_and_borrowed_connection(self):
        with TemporaryDirectory(prefix='stock-ro-files-') as tmp:
            root=Path(tmp);runtime=RegistryUploadDbBackedRuntime(runtime_dir=root)
            runtime.ingest_bundle(json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text()),activated_at='2026-09-07T10:00:00Z')
            book=book_fixture([1,2])
            version=accounting._save_book(root,book,expected=None,operation_id='sidecar-fixture')
            admission=backfill.InventoryHistoryReadAdmission(root,held_fixture(root))
            file=accounting.path(root)
            def inventory(): return sorted(p.name for p in root.glob('fbs-snapshot-accounting.sqlite3*'))
            before=inventory()
            with admission.open(file) as conn:
                self.assertEqual(accounting.load(root,version=version,connection=conn)[1],version)
                self.assertTrue(conn.in_transaction)
                self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0],1)
            self.assertEqual(inventory(),before)
            with closing(sqlite3.connect(file)) as keeper:
                self.assertEqual(keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            # Clean WAL header under confirmed quiet maintenance is safe; no
            # journal is ignored and no WAL/SHM are created by immutable reading.
            self.assertEqual(inventory(),before)
            with admission.open(file) as conn:
                self.assertEqual(accounting.load(root,version=version,connection=conn)[1],version)
            self.assertEqual(inventory(),before)
            with closing(sqlite3.connect(file)) as keeper:
                keeper.execute('BEGIN')
                keeper.execute('SELECT COUNT(*) FROM accounting_revisions').fetchone()
                hot_book=deepcopy(book);hot_book['fixture_hot_wal']=1
                hot_version=accounting._save_book(root,hot_book,expected=version,operation_id='hot-fixture')
                self.assertNotEqual(hot_version,version)
                hot_before=inventory();self.assertIn(file.name+'-wal',hot_before)
                with patch.object(sqlite3,'connect',side_effect=AssertionError('must reject before SQLite open')):
                    with self.assertRaisesRegex(InventoryHistoryBackfillError,'sidecar present'):
                        with admission.open(file): pass
                self.assertEqual(inventory(),hot_before)
            missing=root/'absent'/'missing.sqlite3'
            with self.assertRaisesRegex(InventoryHistoryBackfillError,'missing'):
                with admission.open(missing): pass
            self.assertFalse(missing.parent.exists())
            policy=root/'.auto-updates-policy.json';payload=json.loads(policy.read_text());payload['master_desired']=True;policy.write_text(json.dumps(payload))
            with patch.object(sqlite3,'connect',side_effect=AssertionError('must reject before SQLite open')):
                with self.assertRaisesRegex(InventoryHistoryBackfillError,'held maintenance/control'):
                    with admission.open(file): pass

    def test_wal_commit_readback_pending_keeps_same_operation(self):
        with prepared_backfill_fixture() as (runtime,kwargs,source_file,nms):
            with closing(sqlite3.connect(runtime.db_path)) as writer:
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            dry=run_backfill(**kwargs,apply=False,date_from=DAYS[0],date_to=DAYS[1],bound_sources_path=source_file)
            args=dict(kwargs,manifest_path=Path(dry['manifest_path']),expected_manifest_sha256=dry['manifest_sha256'])
            real_connect=sqlite3.connect;keepers=[]
            def connect(*a,**kw):
                conn=real_connect(*a,**kw)
                if not kw.get('uri') and str(a[0])==str(runtime.db_path):
                    def authorizer(action,table,*rest):
                        if action==sqlite3.SQLITE_INSERT and table==history.APPLIES_TABLE and not keepers:
                            keeper=real_connect(runtime.db_path.as_uri()+'?mode=ro',uri=True)
                            keeper.execute('PRAGMA query_only=ON');keeper.execute('BEGIN')
                            keeper.execute(f'SELECT COUNT(*) FROM {history.APPLIES_TABLE}').fetchone();keepers.append(keeper)
                        return sqlite3.SQLITE_OK
                    conn.set_authorizer(authorizer)
                return conn
            try:
                with patch.object(sqlite3,'connect',side_effect=connect):
                    result=run_backfill(**args,apply=True,approval_reference='local fixture')
                self.assertEqual(result['status'],'committed_readback_pending')
                self.assertFalse(result['retry_apply_allowed'])
                self.assertEqual(result['manifest_sha256'],dry['manifest_sha256'])
                with closing(real_connect(runtime.db_path.as_uri()+'?mode=ro',uri=True)) as conn:
                    self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {history.APPLIES_TABLE} WHERE manifest_hash=?',(dry['manifest_sha256'],)).fetchone()[0],1)
                with self.assertRaisesRegex(InventoryHistoryBackfillError,'sidecar present'):
                    run_backfill(**args,apply=False,readback=True)
            finally:
                for keeper in keepers: keeper.close()

    def test_admission_rejects_live_writer_and_held_book_lock(self):
        import fcntl
        from unittest.mock import Mock
        from apps import business_data_maintenance as maintenance
        with TemporaryDirectory(prefix='stock-os-admission-') as tmp:
            root=Path(tmp);systemd=Mock()
            systemd.unit_state.return_value={'is_enabled':'disabled','is_active':'inactive'}
            systemd.discovered_timers.return_value=list(maintenance.CLASSIFIED_WB_CORE_TIMER_UNITS)
            is_dir=Path.is_dir
            with patch.object(Path,'is_dir',autospec=True,side_effect=lambda p: True if str(p)=='/proc' else is_dir(p)), \
                 patch.object(os,'geteuid',return_value=0), patch.object(maintenance,'SystemdClient',return_value=systemd), \
                 patch.object(maintenance,'_writer_processes',return_value=[]) as processes, \
                 patch.object(maintenance,'_cron_entries',return_value=[]), \
                 patch.object(maintenance,'current_lock_status',return_value={'busy':False}):
                self.assertEqual(backfill._stock_quiet_os_readback(root)['writer_processes'],[])
                self.assertEqual(list(root.iterdir()),[],'admission must not bootstrap locks')
                processes.return_value=[{'pid':123,'marker':'warehouse_functional_runner.py'}]
                with self.assertRaisesRegex(InventoryHistoryBackfillError,'not quiet'):
                    backfill._stock_quiet_os_readback(root)
                processes.return_value=[]
                lock=root/'.fbs-snapshot-accounting.lock';lock.write_text('')
                with lock.open('rb') as held:
                    fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with self.assertRaisesRegex(InventoryHistoryBackfillError,'lock held'):
                        backfill._stock_quiet_os_readback(root)
                systemd.unit_state.return_value={'is_enabled':'enabled','is_active':'active'}
                with self.assertRaisesRegex(InventoryHistoryBackfillError,'not quiet'):
                    backfill._stock_quiet_os_readback(root)

    def browser_check(self, block, expected_version):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from playwright.sync_api import sync_playwright, expect
        fixture=LocalWebVitrinaFixtureServer(with_ready_snapshot=False,now=NOW)
        with fixture as url:
            fixture.entrypoint.web_vitrina_block=block
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                try:
                    page=browser.new_page(viewport={"width":1440,"height":1000})
                    response=page.request.get(url+'/v1/sheet-vitrina-v1/web-vitrina?history_mode=explicit&date_from='+DAYS[0]+'&date_to='+DAYS[1])
                    self.assertEqual(response.status,200)
                    self.assertEqual(response.json()['meta']['inventory_history_version'],expected_version)
                    page.goto(url+'/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from='+DAYS[0]+'&date_to='+DAYS[1])
                    wb=page.locator('td[data-row-id="TOTAL|total_inventory_wb_total_qty_v1"][data-cell-date="'+DAYS[0]+'"]')
                    expect(wb).to_have_text('8',timeout=30000)
                    page.locator('[data-metrics-settings-open]').click()
                    page.locator('[data-total-metric-key="total_'+inventory_planning_facility_metric_key('moscow')+'"] [data-metric-display-select]').select_option('shown')
                    page.locator('[data-metrics-settings-close]').first.click()
                    fbs=page.locator('td[data-row-id="TOTAL|total_'+inventory_planning_facility_metric_key('moscow')+'"][data-cell-date="'+DAYS[0]+'"]')
                    expect(fbs).to_have_text('3',timeout=30000)
                    self.assertIn('Доступно FBS по официальному снимку',fbs.get_attribute('title'))
                    self.assertIn(DAYS[0]+'T18:16:00Z',fbs.get_attribute('title'))
                    destination=os.environ.get('WBC_TYPED_STOCK_SCREENSHOT')
                    if destination: page.screenshot(path=destination,full_page=False)
                finally:
                    browser.close()


if __name__=='__main__':unittest.main()
