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
from packages.application.fbs_snapshot_cost import initialize_candidate,evaluate_candidate,close_candidate_period,fingerprint,canonical
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


def seed_closed_receipts_for_action(fixture,book):
    """LOCAL fixture hook: closed25/26 book plus two later native receipts.

    A fifth SKU isolates the already-posted receipts from the four 583 pools.
    Its official available quantity is explicitly zero on every saved date.
    The receipts use the real intrinsic native planner/writer and immutable
    operator source contract; the repair itself never posts either one again.
    """
    from packages.application.ff_pool_documents import REQUESTS_TABLE,_build_posting_plan,_apply_plan
    from packages.application.ff_pool_foundation import BALANCES_TABLE
    from packages.application import operator_warehouse_documents as operator
    from packages.application.shared_sku_cost_sources import capture_wb_component
    from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
    from packages.application.shared_sku_cost import build_shared_cost_day
    result=deepcopy(book);names=list(range(101,105))+[999]
    with closing(_connect(fixture.runtime.db_path)) as conn:
        operator.ensure_schema(conn)
        fixture.seed_stock(conn,'2026-10-25',(0,0,0,0));fixture.seed_wb(conn,'2026-10-25')
        # Bootstrap this disposable primary-source history before testing its
        # immutability. Restore the exact original triggers before any capture.
        bootstrap_triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?,?)",
            (REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE)).fetchall()
        for trigger in bootstrap_triggers:conn.execute(f'DROP TRIGGER "{trigger["name"]}"')
        chronological=conn.execute(f'SELECT run_id FROM {REGISTRY_RUNS_TABLE} ORDER BY completed_at,run_id').fetchall()
        conn.execute(f'UPDATE {REGISTRY_RUNS_TABLE} SET run_sequence=run_sequence+100')
        for sequence,registry in enumerate(chronological,1):
            conn.execute(f'UPDATE {REGISTRY_RUNS_TABLE} SET run_sequence=? WHERE run_id=?',(sequence,registry['run_id']))
        conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES('999',1,0,999,'Foreign receipt','test','999','[]',?,?)",(STAMP,STAMP))
        for stock in conn.execute(f'SELECT run_id,registry_run_id FROM {STOCK_RUNS_TABLE}').fetchall():
            conn.execute(f'INSERT INTO {STOCK_ROWS_TABLE}(run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest,provenance) VALUES(?,501,1999,999,0,?,\'explicit_wb_row\')',(stock['run_id'],fingerprint([stock['run_id'],999,0])))
        conn.execute(f'UPDATE {STOCK_RUNS_TABLE} SET requested_chrt_count=5,returned_chrt_count=5,explicit_chrt_count=5,dense_row_count=5')
        for registry in conn.execute(f'SELECT run_id,catalog_scope_json FROM {REGISTRY_RUNS_TABLE}').fetchall():
            scope=json.loads(registry['catalog_scope_json']);scope.update(requested_chrt_count=5,active_nm_id_count=5)
            conn.execute(f'UPDATE {REGISTRY_RUNS_TABLE} SET catalog_scope_json=? WHERE run_id=?',(canonical(scope),registry['run_id']))
        for raw_snapshot in conn.execute(f'SELECT * FROM {P}wb_snapshots').fetchall():
            raw=json.loads(raw_snapshot['raw_rows_json']);raw.append(dict(nmId=999,warehouseId=7,stockCount=0,inWayToClient=0,inWayFromClient=0))
            items=json.loads(raw_snapshot['items_json']);items.append(dict(nm_id=999,quantity='0',in_way_to_client='0',in_way_from_client='0',wb_contour_quantity='0'))
            digest=fingerprint(sorted(raw,key=canonical));vid=raw_snapshot['version_id']
            conn.execute(f'UPDATE {P}wb_snapshots SET requested_nm_ids_json=?,raw_row_count=5,raw_rows_digest=?,raw_rows_json=?,items_json=? WHERE version_id=?',
                (canonical(names),digest,canonical(raw),canonical(items),vid))
            version=conn.execute(f'SELECT source_watermarks_json FROM {P}functional_versions WHERE version_id=?',(vid,)).fetchone()
            watermarks=json.loads(version[0]);watermarks['wb_snapshot'].update(digest=digest,raw_row_count=5,requested_count=5)
            conn.execute(f'UPDATE {P}functional_versions SET source_watermarks_json=? WHERE version_id=?',(canonical(watermarks),vid))
        for pool in ('FBS','FBO'):
            conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,999,1,0,\'0\',NULL,\'before-late-receipts\',?)',('synthetic-moscow',pool,STAMP))
        for trigger in bootstrap_triggers:conn.execute(trigger['sql'])
        conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        original=sources._documents(conn,document_id='original07')
        q=archived_quantity_snapshot(conn,day='2026-10-25',observed_at=STAMP)
        old=result['state']['baseline']
        baseline=dict(contract=sources.CONTRACT,business_date='2026-10-25',captured_at=STAMP,
            quantity_snapshot=q,documents_complete=True,documents_reason='',documents=original,source_digest=fingerprint('closed-prefix-fixture'),
            baseline_costs=dict(available=True,version_id='original07',document_manifest={d['document_id']:d['fingerprint'] for d in original},
                rows=[dict(nm_id=n,facility_id='synthetic-moscow',wac_rub=None,source={}) for n in names],
                fbo_rows=list(old['document_cost_state']['fbo_rows'].values())))
        state=initialize_candidate(baseline)
        dated={**baseline,'business_date':'2026-10-26','quantity_snapshot':archived_quantity_snapshot(conn,day='2026-10-26',observed_at=STAMP)}
        dated['source_digest']=fingerprint(dated);state=evaluate_candidate(state,dated);state=close_candidate_period(state,'2026-10-26',today='2026-10-27')
        result.update(state=state,wb_days={},retained_days={},shared_days={},presentations={})
        for day in ('2026-10-25','2026-10-26'):
            wb=capture_wb_component(None,day=day,nm_ids=names,version_id='saved:'+day,connection=conn)
            retained=capture_retained_stages(None,day=day,wb_version_id='saved:'+day,nm_ids=names,connection=conn)
            result['wb_days'][day]=wb;result['retained_days'][day]=retained
            result['shared_days'][day]=build_shared_cost_day(state,wb,day)
            result['presentations'][day]=FbsInventorySnapshot(fbs_state=state,wb_capture=wb,retained=retained,day=day).payload()
        opened={**baseline,'business_date':'2026-10-27','quantity_snapshot':archived_quantity_snapshot(conn,day='2026-10-27',observed_at=STAMP)}
        opened['source_digest']=fingerprint(opened);result['state']=evaluate_candidate(state,opened)
        result['publication_error']=dict(reason='incomplete_period_cannot_close')
        fixture.retained_inputs={day:(deepcopy(result['retained_days'][day]) if day=='2026-10-26' else
            capture_retained_stages(None,day=day,wb_version_id='saved:'+day,nm_ids=names,connection=conn)) for day in DAYS}
        conn.rollback();conn.execute('PRAGMA query_only=OFF');ids=[]
        for label,quantity in (('724',2),('725',3)):
            manifest=dict(facility_id='synthetic-moscow',allocations=[dict(nm_id=999,quantity_fbs=quantity,quantity_fbo=0,
                accepted_quantity=quantity,accepted_capital_rub=str(quantity*100),identity_evidence_digest='sha256:'+'1'*64)])
            request=dict(request_id='closed26:'+label,request_identity=fingerprint(label),client_request_id=label,
                document_kind='china_acceptance',state='ready',source_system='local',source_type='china_acceptance_form',
                source_id='foreign:'+label,source_revision=fingerprint(manifest),idempotency_epoch=1,actor='local',
                business_date='2026-10-26',request_payload_json=canonical(manifest),accepted_at=STAMP,updated_at=STAMP)
            conn.execute(f'INSERT INTO {REQUESTS_TABLE}({",".join(request)}) VALUES({",".join("?" for _ in request)})',tuple(request.values()))
            native_request=conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(request['request_id'],)).fetchone()
            plan=_build_posting_plan(conn,request=native_request,manifest=manifest,epoch=1,intrinsic_only=True)
            source={**{k:native_request[k] for k in operator.SOURCE_KEYS},'manifest':manifest,'source_id':request['source_id'],
                'effect_digest':fingerprint(operator.effect(plan))}
            conn.execute(f'INSERT INTO {operator.TABLE}(request_id,source_json,source_digest,accepted_at,actor,state,updated_at) VALUES(?,?,?,?,?,\'accepted\',?)',
                (request['request_id'],canonical(source),fingerprint(source),STAMP,'local',STAMP))
            _apply_plan(conn,request=native_request,plan=plan,epoch=1,posted_at=STAMP)
            # Intrinsic posting is the inner native transaction. Mirror its
            # outer processor's completion metadata (never another append).
            conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='posted',posted_document_id=?,posted_manifest_sha256=?,posted_at=?,updated_at=? WHERE request_id=? AND state='ready'",
                (plan['primary_document_id'],fingerprint(plan['posted_manifest']),STAMP,STAMP,request['request_id']))
            ids.append(plan['primary_document_id'])
        # Mirror the native acceptance's current-material publication too.
        # Historical editions predate posting; today's aggregate already owns
        # these two receipts and therefore uses their true capture frontier.
        for vid in ('synthetic-aggregate','saved:2026-11-02'):
            provenance=dict(source_records=[dict(source='native_late_receipt_locations',locations=[dict(
                facility_id='synthetic-moscow',pool='FBS',nm_id=999,quantity='5',capital_rub='500')])])
            conn.execute(f'''INSERT INTO {P}functional_balances(version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                VALUES(?,'ff',999,'5','100','500','5','exact',1,'0','0','0',?)''',(vid,canonical(provenance)))
        vid='saved:2026-11-02'
        version=conn.execute(f'SELECT source_watermarks_json FROM {P}functional_versions WHERE version_id=?',(vid,)).fetchone()
        watermarks=json.loads(version[0]);watermarks['captured_at']=STAMP
        conn.execute(f'UPDATE {P}functional_versions SET source_watermarks_json=? WHERE version_id=?',(canonical(watermarks),vid))
        balances=repair._parsed_balances([dict(r) for r in conn.execute(f'SELECT * FROM {P}functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id',(vid,))])
        _materialize_compact_warehouse_read_models(conn,version_id=vid,plan=dict(lines=balances),created_at=STAMP,effective_at=STAMP,business_effective_date='2026-11-02')
        publish_functional_version_business_projection(conn,published_version_id=vid,business_effective_date='2026-11-02',published_at=STAMP,source_revision=fingerprint(vid))
        conn.commit()
    fixture.closed_receipt_ids=sorted(ids)
    return result


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

    def test_missing_balance_is_not_zero_when_stage_totals_disagree(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            row=conn.execute(f"SELECT payload_json FROM {P}functional_read_models WHERE version_id=? AND warehouse_key='production'",('saved:2026-10-31',)).fetchone()
            body=json.loads(row[0]);body['warehouse']['total_quantity']='7'
            conn.execute(f"UPDATE {P}functional_read_models SET payload_json=? WHERE version_id=? AND warehouse_key='production'",(canonical(body),'saved:2026-10-31'))
            conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'saved_stage_totals_changed'):
                repair._saved(repair._Reader(conn),'2026-10-31',version_id='saved:2026-10-31')

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

    def test_reconstructed_positive_stage_does_not_keep_old_zero_proof(self):
        day='2026-10-29';retained=deepcopy(self.capture['retained'][day])
        old_zero=dict(basis='absent_from_complete_published_document_stage',version_id='saved:'+day)
        retained['rows']['101']['stages']['PRODUCTION']['zero_evidence']=old_zero
        balances=deepcopy(self.candidate['editions'][day]['balances'])
        prototype=deepcopy(next(r for r in balances if r['warehouse_key']=='ff' and r['nm_id']==101))
        prototype.update(warehouse_key='production',quantity='10',capital_rub='100',cost_covered_quantity='10',wac_rub='10')
        balances.append(prototype)
        revised=repair._rebuild_retained(retained,wb=self.candidate['candidate_book']['wb_days'][day],balances=balances,
            version_id='new-edition',saved_version='saved:'+day,source_proof='source-proof')
        stage=revised['rows']['101']['stages']['PRODUCTION']
        self.assertEqual(stage['quantity'],'10');self.assertEqual(stage['capital_rub'],'100')
        self.assertNotIn('zero_evidence',stage)
        self.assertEqual(stage['revision_proof']['previous_presentation_zero_evidence'],old_zero)

    def test_later_wb_quantity_above_all_documented_departures_fails(self):
        changed=deepcopy(self.capture);saved=changed['dates']['2026-10-29']
        items=json.loads(saved['snapshot']['items_json']);items[0].update(wb_contour_quantity='9',in_way_from_client='9');saved['snapshot']['items_json']=canonical(items)
        next(r for r in saved['balances'] if r['warehouse_key']=='wb' and r['nm_id']==101)['quantity']='9'
        # Give the numerical guard a self-consistent source binding; the
        # separate source-tamper tests cover rejection of stale digests.
        foreign=changed['foreign_sources'];foreign['saved_digests']['2026-10-29']=fingerprint(saved)
        foreign['source_digest']=fingerprint({k:v for k,v in foreign.items() if k!='source_digest'})
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
class ClosedPrefixReceiptRepairTests(unittest.TestCase):
    def setUp(self):
        local=TemporaryRepairTests();local.prepare_only=True;local.setUp();self.addCleanup(local.doCleanups)
        self.local=local;self.runtime=local.runtime
        self.book=seed_closed_receipts_for_action(local,local.book);self.version=fingerprint(self.book)
        local.book=self.book;local.version=self.version;handoff.load.return_value=(self.book,self.version)
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            allocation=fixture.apply_ordinary_reallocation(conn,'late26-allocation27','2026-10-27',source='FBO',destination='FBS',quantities=dict(zip(range(101,105),fixture.QUANTITIES)))
            conn.commit();conn.execute('BEGIN')
            self.plan=handoff.prepare_historical_handoffs(conn,runtime_dir=self.runtime.runtime_dir,evidence=local.evidence,
                allocation_document_id=allocation,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')
        local.plan=self.plan;local.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            fixture.apply_ordinary_reallocation(conn,'late26-return29','2026-10-29',source='FBS',destination='FBO',quantities=dict(zip(range(101,105),(1592,1788,1192,796))))
            conn.commit()
        self.versions={day:'saved:'+day for day in DAYS};self.now=datetime.fromisoformat(STAMP.replace('Z','+00:00'))

    def capture(self,retained=None):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            return repair.capture_temporary_allocation_repair(conn,runtime_dir=self.runtime.runtime_dir,book=self.book,
                book_version=self.version,native_plan=self.plan,evidence=self.local.evidence,now=self.now,
                source_versions=self.versions,retained_inputs=retained or self.local.retained_inputs)

    def test_two_confirmed_native_receipts_rebuild_frozen26_before_27_without_posting(self):
        from packages.application.fbs_accounting_historical_revision import _prefix
        before=deepcopy(self.book)
        with closing(_connect(self.runtime.db_path)) as conn:
            counts=tuple(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in (
                'sheet_vitrina_v1_warehouse_business_operations','sheet_vitrina_v1_ff_pool_documents'))
        captured=self.capture();proof=captured['closed_prefix_repair']
        self.assertEqual(proof['contract'],repair.CLOSED_PREFIX_CONTRACT)
        self.assertEqual(proof['receipt_document_ids'],self.local.closed_receipt_ids)
        self.assertEqual(captured['quantity_snapshots']['2026-10-26'],self.book['state']['periods']['2026-10-26']['snapshot'])
        candidate=repair.build_temporary_allocation_repair(self.book,capture=captured,native_plan=self.plan)
        self.assertEqual(self.book,before)
        self.assertEqual(min(candidate['editions']),'2026-10-26')
        self.assertEqual(_prefix(candidate['candidate_book'],'2026-10-26'),_prefix(self.book,'2026-10-26'))
        self.assertEqual(candidate['quality_proof']['closed_prefix_repair'],proof)
        applied=candidate['candidate_book']['state']['periods']['2026-10-26']['applied_documents']
        self.assertTrue(set(self.local.closed_receipt_ids)<=set(applied))
        self.assertEqual(candidate['candidate_book']['state']['pending_documents'],[])
        self.assertTrue(all(a['business_date']!='2026-10-26' for a in candidate['initial_anchors'].values()))
        self.assertFalse(any(row['as_of_date']=='2026-10-26' for row in candidate['daily_wb_costs']))
        self.assertTrue(proof['query_fence'])
        self.assertTrue(all(fence in candidate['foreign_source_fence'] for fence in proof['query_fence']))
        with closing(_connect(self.runtime.db_path)) as conn:
            self.assertEqual(counts,tuple(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in (
                'sheet_vitrina_v1_warehouse_business_operations','sheet_vitrina_v1_ff_pool_documents')))

    def test_unknown_late_document_is_not_silently_added_to_the_cohort(self):
        captured=self.capture();changed=deepcopy(captured['accounting_capture'])
        extra=deepcopy(next(d for d in changed['documents'] if d['document_id'] in self.local.closed_receipt_ids))
        extra.update(document_id='unapproved-third',fingerprint=fingerprint('unapproved-third'))
        changed['documents'].append(extra)
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'closed_prefix_unapproved_pending'):
                repair._closed_prefix_receipts(conn,repair._Reader(conn),book=self.book,accounting=changed,native_plan=self.plan,
                    source_versions=self.versions,retained_inputs=self.local.retained_inputs)

    def test_frozen_retained_and_operator_confirmation_cannot_be_replaced(self):
        changed=deepcopy(self.local.retained_inputs);changed['2026-10-26']['rows']['101']['stages']['PRODUCTION']['quantity']='1'
        changed['2026-10-26']['source_digest']=fingerprint({k:v for k,v in changed['2026-10-26'].items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'closed_prefix_frozen_stage_changed'):self.capture(changed)
        captured=self.capture();proof=deepcopy(captured['closed_prefix_repair']);proof['frozen_quantity_digest']='changed'
        proof['source_digest']=fingerprint({k:v for k,v in proof.items() if k!='source_digest'})
        captured['closed_prefix_repair']=proof;captured['source_digest']=fingerprint({k:v for k,v in captured.items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'closed_prefix_proof_changed'):
            repair.build_temporary_allocation_repair(self.book,capture=captured,native_plan=self.plan)


if __name__=='__main__':unittest.main()
