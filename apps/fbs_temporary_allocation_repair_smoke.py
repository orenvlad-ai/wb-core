#!/usr/bin/env python3
"""Real local 27/32/29 native documents, true-shaped WB14 and daily book replay."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import ff_pool_fbs_historical_handoff_smoke as fixture
from packages.application import fbs_temporary_allocation_repair as repair
from packages.application import wb_historical_fbs_cost as initial
from packages.application import ff_pool_fbs_historical_handoff as handoff
from packages.application import fbs_snapshot_cost_sources as sources
from packages.application.fbs_snapshot_cost import initialize_candidate,evaluate_candidate,fingerprint,canonical
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.warehouse_functional import _stable_id,_materialize_compact_warehouse_read_models
from packages.application.warehouse_business_projection import ensure_warehouse_business_projection_schema,ensure_functional_version_business_time_schema,publish_functional_version_business_projection
from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE,WAREHOUSE_MAPPINGS_TABLE,COMPLETE_CATALOG_OMISSION_ZERO_POLICY
from packages.application.fbs_accounting_historical_sources import archived_quantity_snapshot
from packages.application.fbs_inventory_presentation import capture_retained_stages
from packages.application.ff_pool_foundation import canonical_decimal_ratio_text

STAMP=fixture.STAMP
DAYS=['2026-10-26','2026-10-27','2026-10-28','2026-10-29','2026-10-30','2026-10-31','2026-11-01','2026-11-02']
P=repair.P

class TemporaryRepairTests(unittest.TestCase):
    apply=fixture.HistoricalHandoffTests.apply
    def seed_stock(self,conn,day,quantities):
        warehouse=dict(facility_id='synthetic-moscow',mapping_id='wm',seller_warehouse_id=501)
        gid='official:'+day;sid='stock:'+day;stamp=day+'T14:50:00Z'
        conn.execute(f'INSERT INTO {REGISTRY_RUNS_TABLE}(run_id,status,complete,started_at,completed_at,warehouse_count,office_count,source_digest,policy_version,catalog_scope_json,warehouse_scope_json,generation_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (gid,'success',1,stamp,day+'T14:51:00Z',1,1,fingerprint(gid),COMPLETE_CATALOG_OMISSION_ZERO_POLICY,canonical(dict(complete=True,requested_chrt_count=4,active_nm_id_count=4)),canonical(dict(complete=True,warehouse_count=1,warehouses=[warehouse])),fingerprint([gid,'complete'])))
        conn.execute(f'INSERT INTO {STOCK_RUNS_TABLE}(run_id,registry_run_id,seller_warehouse_id,status,complete,snapshot_at,requested_chrt_count,returned_chrt_count,identity_scope_json,source_digest,policy_version,explicit_chrt_count,omitted_zero_count,dense_row_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (sid,gid,501,'success',1,stamp,4,4,'{}',fingerprint(sid),COMPLETE_CATALOG_OMISSION_ZERO_POLICY,4,0,4))
        for nm,q in zip(range(101,105),quantities):
            conn.execute(f'INSERT INTO {STOCK_ROWS_TABLE}(run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest,provenance) VALUES(?,?,?,?,?,?,?)',(sid,501,nm+1000,nm,q,fingerprint([sid,nm,q]),'explicit_wb_row'))
    def seed_wb(self,conn,day):
        vid='saved:'+day;stamp=day+'T12:00:00Z';qs=(4,7,3,0) if day>='2026-10-28' else (0,0,0,0)
        raw=[dict(nmId=nm,warehouseId=7,stockCount=0,inWayToClient=0,inWayFromClient=q) for nm,q in zip(range(101,105),qs)]
        items=[dict(nm_id=nm,quantity='0',in_way_to_client='0',in_way_from_client=str(q),wb_contour_quantity=str(q)) for nm,q in zip(range(101,105),qs)]
        source='source:'+vid;digest=fingerprint(sorted(raw,key=canonical))
        watermark=dict(snapshot_id=source,digest=digest,fetched_at=stamp,pagination_complete=True,raw_row_count=4,requested_count=4)
        conn.execute(f'INSERT INTO {P}functional_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(vid,repair.wf.FUNCTIONAL_CUTOVER_ID,'hourly_wb_sync',stamp,'good',fingerprint(vid),fingerprint([vid,'source']),canonical(dict(wb_snapshot=watermark,captured_at=stamp)),stamp,day,stamp))
        conn.execute(f'INSERT INTO {P}wb_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(_stable_id('wbsnapv',dict(source_snapshot_id=source,version_id=vid)),vid,stamp,day,canonical(list(range(101,105))),1,1,'[0]',4,digest,canonical(raw),canonical(items),stamp))
        lines=[]
        for nm,q,original,cap in zip(range(101,105),qs,fixture.QUANTITIES,fixture.CAPITALS):
            for key in ('wb','ff'):
                ff=key=='ff';qty=str(original if ff else q)
                locations=[dict(facility_id='synthetic-moscow',pool='FBO',nm_id=nm,quantity=str(original),capital_rub=cap)] if ff else []
                prov=dict(source_records=[dict(source='native_original_location' if ff else 'official_wb_snapshot',locations=locations,snapshot_id=source,snapshot_date=day,fetched_at=stamp)])
                line=dict(warehouse_key=key,nm_id=nm,quantity=qty,wac_rub=canonical_decimal_ratio_text(cap,original) if ff else None,capital_rub=cap if ff else '0',cost_covered_quantity=qty if ff else '0',quality='saved' if ff else 'missing_cost' if q else 'zero',certified=0,wb_quantity='0',wb_in_way_to_client='0',wb_in_way_from_client=str(q) if not ff else '0',provenance=prov)
                lines.append(line)
                conn.execute(f'INSERT INTO {P}functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(vid,key,nm,qty,line['wac_rub'],line['capital_rub'],line['cost_covered_quantity'],line['quality'],0,'0','0',line['wb_in_way_from_client'],canonical(prov)))
        _materialize_compact_warehouse_read_models(conn,version_id=vid,plan=dict(lines=lines),created_at=stamp,effective_at=stamp,business_effective_date=day)
        publish_functional_version_business_projection(conn,published_version_id=vid,business_effective_date=day,published_at=stamp,source_revision=fingerprint(vid))
    def setUp(self):
        fixture.HistoricalHandoffTests.setUp(self)
        if getattr(self,'prepare_only',False):
            import shutil
            shutil.copyfile(self.pre27,self.runtime.db_path)
        with closing(_connect(self.runtime.db_path)) as conn:
            ensure_warehouse_business_projection_schema(conn);ensure_functional_version_business_time_schema(conn)
            for nm in range(101,105):
                conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES(?,1,0,?,'Synthetic','test',?,'[]',?,?)",(str(nm),nm,str(nm),STAMP,STAMP))
            for day in DAYS:
                # Local source-shaped daily availability; no handoff subtraction in replay.
                quantities=(1600,1798,1200,800) if day=='2026-10-27' else (1596,1792,1195,800) if day=='2026-10-28' else (0,0,0,0)
                self.seed_stock(conn,day,quantities);self.seed_wb(conn,day)
            conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            original=sources._documents(conn,document_id='original07')
            q=archived_quantity_snapshot(conn,day=DAYS[0],observed_at=STAMP)
            base=dict(contract=sources.CONTRACT,business_date=DAYS[0],captured_at=STAMP,quantity_snapshot=q,documents_complete=True,documents_reason='',documents=original,source_digest=fingerprint('baseline'),baseline_costs=dict(available=True,version_id='original07',document_manifest={d['document_id']:d['fingerprint'] for d in original},rows=[dict(nm_id=n,facility_id='synthetic-moscow',wac_rub=None,source={}) for n in range(101,105)],fbo_rows=[dict(nm_id=n,facility_id='synthetic-moscow',pool='FBO',quantity=str(qty),capital_rub=cap,wac_rub=canonical_decimal_ratio_text(cap,qty),source=dict(document_id='original07')) for n,qty,cap in zip(range(101,105),fixture.QUANTITIES,fixture.CAPITALS)]))
            state=initialize_candidate(base)
            failed=deepcopy(base);failed['business_date']='2026-10-27';failed['quantity_snapshot']=archived_quantity_snapshot(conn,day='2026-10-27',observed_at=STAMP);failed['source_digest']=fingerprint(failed)
            state=evaluate_candidate(state,failed)
            self.book=dict(schema=handoff.SCHEMA,active=True,state=state,wb_days={},retained_days={},shared_days={},presentations={},publication_error=dict(reason='incomplete_period_cannot_close'))
            self.version=fingerprint(self.book);handoff.load.return_value=(self.book,self.version)
            self.retained_inputs={d:capture_retained_stages(None,day=d,wb_version_id='saved:'+d,nm_ids=list(range(101,105)),connection=conn) for d in DAYS[1:]}
            if getattr(self,'prepare_only',False):return
            self.plan=handoff.prepare_historical_handoffs(conn,runtime_dir=self.runtime.runtime_dir,evidence=self.evidence,allocation_document_id=self.allocation_id,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')
        self.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.return_id=fixture.apply_ordinary_reallocation(conn,'return29','2026-10-29',source='FBS',destination='FBO',quantities=dict(zip(range(101,105),(1592,1788,1192,796))))
            conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            self.capture=repair.capture_temporary_allocation_repair(conn,runtime_dir=self.runtime.runtime_dir,book=self.book,book_version=self.version,native_plan=self.plan,evidence=self.evidence,now=datetime.fromisoformat(STAMP.replace('Z','+00:00')),source_versions={d:'saved:'+d for d in DAYS},retained_inputs=self.retained_inputs)
        self.candidate=repair.build_temporary_allocation_repair(self.book,capture=self.capture,native_plan=self.plan)
    def test_real_documents_wb14_and_zero_return_daily_shared(self):
        c=self.candidate;b=c['candidate_book']
        self.assertNotIn('publication_error',b)
        self.assertEqual(len(c['initial_anchors']),3)
        for edition in c['editions'].values():
            self.assertEqual(edition['balances'],sorted(edition['balances'],key=lambda row:(row['warehouse_key'],row['nm_id'])))
        self.assertEqual(c['quality_proof']['pending_post_handoff_cancellations'],[1001])
        for day in DAYS[1:]:
            self.assertEqual(b['shared_days'][day]['quality'],'complete')
            self.assertEqual(b['presentations'][day]['totals']['total']['capital_rub'] is not None,True)
        wb=b['wb_days']['2026-10-28'];self.assertEqual(sum(Decimal(r['quantity']) for r in wb['rows']),14)
        for anchor in c['initial_anchors'].values():self.assertTrue(initial.valid_historical_anchor(anchor));self.assertFalse(anchor['proves_specific_order_return'])
        shared=b['shared_days']['2026-10-29']
        for row in shared['rows'].values():
            parts={p['component']:p for p in row['components']}
            self.assertEqual(Decimal(parts['FF_FBS']['quantity']),0)
            self.assertEqual(Decimal(row['quantity']),Decimal(parts['FF_FBO']['quantity'])+Decimal(parts['WB']['quantity']))
            with localcontext() as context:
                context.prec=50
                self.assertEqual(Decimal(row['capital_rub']),Decimal(parts['FF_FBO']['capital_rub'])+Decimal(parts['WB']['capital_rub']))
        self.assertEqual(sum(Decimal(r['quantity']) for r in shared['rows'].values()),5382)
        final=b['state']['periods']['2026-10-29'];self.assertTrue(all(Decimal(r['quantity'])==0 and Decimal(r['capital_rub'])==0 for r in final['rows'].values()))
        self.assertEqual(sum(Decimal(r['quantity']) for r in final['document_cost_state']['fbo_rows'].values()),5368)
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE');repair.publish_temporary_allocation_stages(conn,manifest=c);conn.commit()
            self.assertTrue(repair.verify_temporary_allocation_stage_readback(conn,candidate=c))
    def test_pre_native_retained_binding_cannot_change(self):
        changed=deepcopy(self.retained_inputs);changed['2026-10-28']['wb_version_id']='another-version'
        changed['2026-10-28']['source_digest']=fingerprint({k:v for k,v in changed['2026-10-28'].items() if k!='source_digest'})
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'pre_native_retained_binding_changed'):
                repair.capture_temporary_allocation_repair(conn,runtime_dir=self.runtime.runtime_dir,book=self.book,book_version=self.version,native_plan=self.plan,evidence=self.evidence,now=datetime.fromisoformat(STAMP.replace('Z','+00:00')),source_versions={d:'saved:'+d for d in DAYS},retained_inputs=changed)

    def test_foreign_unknown_retained_payload_is_never_repaired(self):
        retained=deepcopy(self.capture['retained']['2026-10-28'])
        foreign=deepcopy(retained['rows']['101']);foreign['reason']='foreign_stage_incomplete'
        foreign['stages']['PRODUCTION'].update(quantity=None,capital_rub=None,wac_rub=None)
        retained['rows']['999']=foreign
        selected_before=deepcopy(retained['rows']['101']['stages']['PRODUCTION'])
        revised=repair._revise_retained(retained,wb=self.candidate['candidate_book']['wb_days']['2026-10-28'],selected=[101,102,103,104],version_id='revision',saved_version='before')
        self.assertEqual(revised['rows']['999'],foreign)
        self.assertEqual(revised['rows']['101']['stages']['PRODUCTION'],selected_before)
        changed=deepcopy(self.capture)
        changed['retained']['2026-10-28']['rows']['101']['stages']['PRODUCTION'].update(quantity=None,capital_rub=None,wac_rub=None)
        changed['source_digest']=fingerprint({k:v for k,v in changed.items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'shared_cost_incomplete'):repair.build_temporary_allocation_repair(self.book,capture=changed,native_plan=self.plan)

    def test_later_wb_quantity_above_all_documented_departures_fails(self):
        changed=deepcopy(self.capture);saved=changed['dates']['2026-10-29']
        items=json.loads(saved['snapshot']['items_json']);items[0].update(wb_contour_quantity='9',in_way_from_client='9');saved['snapshot']['items_json']=canonical(items)
        next(r for r in saved['balances'] if r['warehouse_key']=='wb' and r['nm_id']==101)['quantity']='9'
        changed['source_digest']=fingerprint({k:v for k,v in changed.items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'wb_quantity_exceeds_documented_handoffs'):repair.build_temporary_allocation_repair(self.book,capture=changed,native_plan=self.plan)

    def test_missing_continuity_fails_and_ordinary_anchor_validation_remains_strict(self):
        from packages.application.wb_initial_fbs_cost import valid_anchor,resolve
        self.assertFalse(valid_anchor(None))
        self.assertFalse(resolve(101,{},None,business_date='2026-10-28',snapshot_id='x',fetched_at=STAMP,previous_version='x')['available'])
        self.assertTrue(all(valid_anchor(a) for a in self.candidate['initial_anchors'].values()))
        changed=deepcopy(self.capture);changed['cost_interval_proof']['complete']=False
        changed['source_digest']=fingerprint({k:v for k,v in changed.items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'cost_continuity_unproven'):repair.build_temporary_allocation_repair(self.book,capture=changed,native_plan=self.plan)

    def test_unknown_physical_or_tampered_documentary_basis_fails(self):
        changed=deepcopy(self.capture);saved=changed['dates']['2026-10-28'];items=json.loads(saved['snapshot']['items_json']);items[0]['quantity']='4';items[0]['in_way_from_client']='0';saved['snapshot']['items_json']=canonical(items);changed['source_digest']=fingerprint({k:v for k,v in changed.items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'physical_stock_source_unproved'):repair.build_temporary_allocation_repair(self.book,capture=changed,native_plan=self.plan)
        anchor=deepcopy(next(iter(self.candidate['initial_anchors'].values())));anchor['documentary_basis']['original_capital_rub']='1'
        self.assertFalse(initial.valid_historical_anchor(anchor))
if __name__=='__main__':unittest.main()
