#!/usr/bin/env python3
"""Disposable SQLite tests for reviewed dated handoffs; no external calls."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import ff_pool_fbs_historical_handoff as historical
from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application import wb_fbs_orders as orders
from packages.application import ff_pool_documents as docs
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, DOCUMENT_LINES_TABLE, REQUESTS_TABLE
from packages.application.ff_pool_foundation import FACILITIES_TABLE, FEATURE_EPOCHS_TABLE, BALANCES_TABLE, OPERATIONS_TABLE, LINES_TABLE, canonical_decimal_ratio_text
from packages.application.fbs_snapshot_cost import fingerprint, canonical
from packages.application.registry_upload_db_backed_runtime import _connect, _ensure_schema
from packages.application.warehouse_functional import ensure_warehouse_functional_schema
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
from packages.application.fbs_accounting_runtime import writer_lock

STAMP = '2026-11-02T15:00:00Z'
CAPITALS = ('214330.00', '241121.26', '157678.51', '105119.01')
QUANTITIES = (1600, 1800, 1200, 800)
VECTORS = ((4,7,3,0), (0,1,2,0), (2,2,2,3), (1,0,1,0), (1,2,0,1))


def evidence_fixture(*, duplicate_status=False, observed_at=STAMP):
    raw, statuses, groups = [], [], []
    oid = 1000
    for g, vector in enumerate(VECTORS):
        supply = 'WB-GI-synthetic-' + str(g)
        anchor = oid
        for nm, quantity in zip(range(101,105), vector):
            for _ in range(quantity):
                raw.append(dict(id=oid, createdAt='2026-10-27T12:00:00Z', supplyId=supply, deliveryType='fbs',
                    nmId=nm, chrtId=nm+1000, warehouseId=501, officeId=601, article='seller-'+str(nm), skus=['sku-'+str(nm)]))
                statuses.append(dict(order_id=oid, supplier_status='complete', wb_status='canceled_by_client' if oid == 1001 else
                    ('sorted' if oid%8 == 0 else 'sold' if oid%2 else 'ready_for_pickup')))
                oid += 1
        groups.append(dict(supply_id=supply, anchor_order_id=anchor, wave_id=str(2000+g), shipment_id=str(3000+g),
            business_date='2026-10-28' if g<2 else '2026-10-29', authority='direct_order_wave_shipment',
            date_authority='shipment_or_mh3_business_date', order_source_file='order'+str(g), shipment_source_file='shipment'+str(g),
            date_source_file='shipment'+str(g), quantity_by_nm={str(n):q for n,q in zip(range(101,105),vector) if q}))
    raw.append(dict(id=oid, createdAt='2026-10-29T12:00:00Z', deliveryType='fbs', nmId=101, chrtId=1101,
        warehouseId=501, officeId=601, article='seller-101', skus=['sku-101']))
    statuses.append(dict(order_id=oid,supplier_status='new',wb_status='declined_by_client'))
    if duplicate_status:statuses.append(dict(order_id=1001,supplier_status='complete',wb_status='sold'))
    official=dict(at=observed_at, date_from='2026-10-26T00:00:00Z',date_to=STAMP,orders=raw,statuses=statuses,
        pages=[dict(cursor=0,next_cursor=50,returned=33)])
    terminal=dict(date_to=STAMP,cursor=50,returned=0,next_cursor=0,coverage_terminal=True,orders=[],statuses=[])
    return historical.build_handoff_evidence(official,terminal,groups=groups,
        source_files={key:'sha256:'+str(i%10)*64 for i,key in enumerate([k for g in range(5) for k in ('order'+str(g),'shipment'+str(g))])},
        approval_reference='local-reviewed-primary-proof')


def seed_document(conn, name, day, kind, pool, *, movements):
    manifest=dict(domain_manifest=dict(facility_id='synthetic-moscow',source_pool='FBO',destination_pool='FBS',
        physical_total_unchanged=True,expense_rub='0.00'), synthetic=True)
    conn.execute(f'''INSERT INTO {REQUESTS_TABLE}(request_id,request_identity,client_request_id,document_kind,state,source_system,source_type,source_id,
        source_revision,idempotency_epoch,actor,business_date,request_payload_json,accepted_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,1,'local',?,'{{}}',?,?)''',
        (name,name,name,kind,'complete','synthetic','source',name,fingerprint(name),day,STAMP,STAMP))
    conn.execute(f'INSERT INTO {OPERATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
        (name,kind,'synthetic','source',name,fingerprint(name),1,day,STAMP,'{}'))
    conn.execute(f'''INSERT INTO {DOCUMENTS_TABLE}(document_id,request_id,document_role,document_kind,root_document_id,operation_id,
        source_system,source_type,source_id,source_revision,idempotency_epoch,actor,business_date,posted_manifest_sha256,posted_manifest_json,posted_at)
        VALUES(?,?,'root',?,?,?,'synthetic','source',?,?,1,'local',?,?,?,?)''',
        (name,name,kind,name,name,name,fingerprint(name),day,fingerprint(manifest),canonical(manifest),STAMP))
    line_no=0
    for nm,q,c in zip(range(101,105),QUANTITIES,CAPITALS):
        conn.execute(f'INSERT INTO {DOCUMENT_LINES_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (name,nm-100,name,'reallocated' if kind=='pool_reallocation' else 'accepted_pool_allocation','synthetic-moscow',pool,nm,q,c,'0','{}'))
        for movement_pool, sign in movements:
            line_no += 1
            conn.execute(f'INSERT INTO {LINES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',
                (name,line_no,'synthetic-moscow',movement_pool,nm,q*sign,
                 format(Decimal(c).copy_negate() if sign<0 else Decimal(c),'f'),canonical_decimal_ratio_text(c,q),'{}'))


def apply_ordinary_reallocation(conn, name, day, *, source, destination, quantities):
    payload=dict(facility_id='synthetic-moscow',source_pool=source,destination_pool=destination,
                 items=[dict(nm_id=nm,quantity=q) for nm,q in sorted(quantities.items())],expenses=[])
    conn.execute(f'''INSERT INTO {REQUESTS_TABLE}(request_id,request_identity,client_request_id,document_kind,state,source_system,source_type,source_id,
        source_revision,idempotency_epoch,actor,business_date,request_payload_json,accepted_at,updated_at)
        VALUES(?,?,?,'pool_reallocation','ready','synthetic','source',?,?,1,'local',?,?,?,?)''',
        (name,fingerprint(name),name,name,fingerprint(payload),day,canonical(payload),STAMP,STAMP))
    request=conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(name,)).fetchone()
    plan=docs._build_posting_plan(conn,request=request,manifest=payload,epoch=1)
    docs._apply_plan(conn,request=request,plan=plan,epoch=1,posted_at=STAMP)
    return plan['primary_document_id']


class HistoricalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=SimpleNamespace(runtime_dir=Path(self.tmp.name),db_path=Path(self.tmp.name)/'main.sqlite3')
        self.book=dict(schema=historical.SCHEMA,active=True,state=dict(baseline=dict(business_date='2026-10-26',rows={}),
            periods={'2026-10-27':dict(status='incomplete',rows={})}),publication_error=dict(reason='incomplete_period_cannot_close'))
        self.version=fingerprint(self.book)
        self.load=patch.object(historical,'load',return_value=(self.book,self.version));self.load.start();self.addCleanup(self.load.stop)
        self.evidence=evidence_fixture()
        with closing(_connect(self.runtime.db_path)) as conn:
            _ensure_schema(conn);ensure_warehouse_functional_schema(conn)
            conn.execute(f'INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,?,?,?)',('synthetic-moscow','SYN','Synthetic',1,'UTC',STAMP,STAMP))
            conn.execute(f'INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,1,1,?,?,?)',('synthetic-epoch',STAMP,'{}'))
            for nm,q,c in zip(range(101,105),QUANTITIES,CAPITALS):
                conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',('synthetic-moscow','FBS',nm,1,0,'0',None,'original07',STAMP))
                conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',('synthetic-moscow','FBO',nm,1,q,c,canonical_decimal_ratio_text(c,q),'original07',STAMP))
                conn.execute(f'''INSERT INTO {orders.IDENTITY_MAPPINGS_TABLE}(mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,target_nm_id,
                    mapping_digest,active,created_at,created_by) VALUES(?,?,?,?,?,?,?,1,?,'local')''',
                    ('im'+str(nm),nm,nm+1000,'sku-'+str(nm),'seller-'+str(nm),nm,fingerprint(nm),STAMP))
            conn.execute(f'''INSERT INTO {orders.WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES('wm',501,'synthetic-moscow',?,1,?,'local')''',(fingerprint('wm'),STAMP))
            seed_document(conn,'original07','2026-10-07','china_acceptance','FBO',movements=[('FBO',1)])
            manifest=dict(cutover_id='synthetic-cutover',feature_epoch=1,business_date='2026-08-14',cutover_at='2026-08-14T05:00:00Z',
                aggregate_revision='synthetic-aggregate',handoff_policy=dict(approved=True,supplier_status='complete',wb_status='sorted',approval_reference='local',supplier_status_complete_alone_forbidden=True))
            fields=[r[1] for r in conn.execute('PRAGMA table_info(sheet_vitrina_v1_ff_pool_cutover_manifests)')]
            values={key:'synthetic' for key in fields};values.update(cutover_id=manifest['cutover_id'],manifest_digest=fingerprint(manifest),deployed_sha='a'*40,
                cutover_at='2026-08-14T05:00:00Z',business_date='2026-08-14',feature_epoch=1,observation_watermark_sequence=0,created_at=STAMP,manifest_json=canonical(manifest))
            conn.execute(f"INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",[values[k] for k in fields])
            # No functional version row: the existing exact opening aggregate
            # seam is exercised here. Full publisher/replay is a separate test.
            conn.execute('INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,?,?)',('synthetic-aggregate',STAMP))
            for nm,q,c in zip(range(101,105),QUANTITIES,CAPITALS):
                conn.execute('''INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                    cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json)
                    VALUES(?,'ff',?,?,?,?,?,'exact',1,'0','0','0','{}')''',('synthetic-aggregate',nm,str(q),canonical_decimal_ratio_text(c,q),c,str(q)))
            conn.commit()
            self.pre27=self.runtime.runtime_dir/'pre27.sqlite3'
            with sqlite3.connect(self.pre27) as backup:conn.backup(backup)
            conn.execute('BEGIN IMMEDIATE')
            self.allocation_id=apply_ordinary_reallocation(conn,'allocation27','2026-10-27',source='FBO',destination='FBS',quantities=dict(zip(range(101,105),QUANTITIES)))
            conn.commit();conn.execute('BEGIN')
            self.plan=historical.prepare_historical_handoffs(conn,runtime_dir=self.runtime.runtime_dir,evidence=self.evidence,
                allocation_document_id=self.allocation_id,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')

    def apply(self, **kw):
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner, warehouse_functional_write_lock(self.runtime.runtime_dir),writer_lock(self.runtime.runtime_dir):
            with closing(_connect(self.runtime.db_path)) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    result=historical.apply_historical_handoffs(conn,self.plan,runtime=self.runtime,owner_token=owner['owner_token'],**kw)
                    self.assertTrue(conn.in_transaction)
                    conn.commit();return result
                except BaseException:
                    conn.rollback();raise

    def snapshot(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            return {table:[tuple(row) for row in conn.execute('SELECT * FROM '+table)] for table in
                (BALANCES_TABLE,OPERATIONS_TABLE,LINES_TABLE,lifecycle.EVENTS_TABLE,lifecycle.CURRENT_TABLE,orders.OBSERVATIONS_TABLE,
                 orders.STATUS_OBSERVATIONS_TABLE,orders.STATUS_CURRENT_TABLE,orders.IDENTITY_EVIDENCE_TABLE,'sheet_vitrina_v1_warehouse_functional_balances')}

    def test_32_actual_cost_profile_dates_current_statuses_and_repeat(self):
        self.assertEqual(self.apply()['physical_debits'],32)
        before=self.snapshot();self.assertTrue(self.apply()['idempotent']);self.assertEqual(before,self.snapshot())
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN')
            self.assertEqual(historical.read_historical_handoff_plan(conn),self.plan)
            dates=dict(conn.execute(f"SELECT business_date,COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff' GROUP BY business_date"))
            self.assertEqual(dates,{'2026-10-28':17,'2026-10-29':15})
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {orders.OBSERVATIONS_TABLE}').fetchone()[0],33)
            self.assertEqual(conn.execute(f'SELECT state FROM {lifecycle.CURRENT_TABLE} WHERE order_id=1032').fetchone()[0],'cancelled_noop')
            self.assertEqual(tuple(conn.execute(f'SELECT supplier_status,wb_status FROM {lifecycle.CURRENT_TABLE} WHERE order_id=1001').fetchone()),('complete','canceled_by_client'))
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {orders.STATUS_CURRENT_TABLE} WHERE source_observed_at<>''").fetchone()[0],0)
            for nm,q,c,count in zip(range(101,105),QUANTITIES,CAPITALS,(8,12,8,4)):
                row=conn.execute(f'SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} WHERE pool=\'FBS\' AND nm_id=?',(nm,)).fetchone()
                with localcontext() as context:
                    context.prec=160
                    self.assertEqual(Decimal(row[1])+Decimal(self.plan['sources'][str(nm)]['wac_rub'])*count,Decimal(c))
                self.assertEqual(row[0],q-count);self.assertLessEqual(len(row[2]),80)

    def test_offset_observations_persist_with_same_instant_and_identity(self):
        expected='2026-11-02T15:00:00.850115Z'
        self.evidence=evidence_fixture(observed_at=expected)
        for stamp in ('2026-11-02T15:00:00.850115+00:00', '2026-11-02T19:00:00.850115+04:00'):
            equivalent=evidence_fixture(observed_at=stamp)
            self.assertEqual(equivalent['orders'],self.evidence['orders'])
            self.assertNotEqual(equivalent['official_payload_digest'],self.evidence['official_payload_digest'])
        self.evidence=equivalent
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN')
            self.plan=historical.prepare_historical_handoffs(conn,runtime_dir=self.runtime.runtime_dir,evidence=self.evidence,
                allocation_document_id=self.allocation_id,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')
        self.assertEqual(self.apply()['physical_debits'],32)
        with closing(_connect(self.runtime.db_path)) as conn:
            for table in (orders.OBSERVATIONS_TABLE,orders.STATUS_OBSERVATIONS_TABLE):
                self.assertEqual([tuple(row) for row in conn.execute(f'SELECT observed_at,COUNT(*) FROM {table} GROUP BY observed_at')],[(expected,33)])
        before=self.snapshot();self.assertTrue(self.apply()['idempotent']);self.assertEqual(before,self.snapshot())

    def test_observation_without_timezone_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'observation_timezone_required'):
            evidence_fixture(observed_at='2026-11-02T15:00:00')

    def test_every_physical_boundary_and_events_rollback(self):
        before=self.snapshot()
        for boundary in [('after_events',0)]+[('after_physical',i) for i in range(32)]:
            def fail(phase,index):
                if (phase,index)==boundary:raise RuntimeError('injected')
            with self.assertRaisesRegex(RuntimeError,'injected'):self.apply(fault=fail)
            self.assertEqual(before,self.snapshot())

    def test_drift_book_reservation_and_changed_proof_rejected(self):
        before=self.snapshot();historical.load.return_value=(self.book,'drift')
        with self.assertRaisesRegex(ValueError,'book_compare_and_swap_failed'):self.apply()
        self.assertEqual(before,self.snapshot());historical.load.return_value=(self.book,self.version)
        changed=deepcopy(self.plan);changed['orders'][0]['dispatch']['business_date']='2026-10-30';old=self.plan;self.plan=changed
        with self.assertRaisesRegex(ValueError,'plan_digest_invalid'):self.apply()
        self.plan=old
        with closing(_connect(self.runtime.db_path)) as conn,conn:
            conn.execute(f"UPDATE {BALANCES_TABLE} SET source_watermark='foreign' WHERE nm_id=101 AND pool='FBS'")
        with self.assertRaisesRegex(ValueError,'native_compare_and_swap_failed'):self.apply()

    def test_foreign_reserved_stock_blocks_preparation_without_writes(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(f'''INSERT INTO {lifecycle.CURRENT_TABLE}(cutover_id,order_id,state,episode_sequence,source_revision,status_digest,supplier_status,
                wb_status,facility_id,pool,nm_id,quantity,frozen_wac_rub,debit_event_id,updated_at)
                VALUES('synthetic-cutover',9998,'reserved',1,'synthetic','synthetic','new','waiting','synthetic-moscow','FBS',101,1593,'1','',?)''',(STAMP,))
            before=conn.total_changes
            with self.assertRaises(Exception) as caught:
                historical.prepare_historical_handoffs(conn,runtime_dir=self.runtime.runtime_dir,evidence=self.evidence,
                    allocation_document_id=self.allocation_id,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')
            self.assertEqual(getattr(caught.exception,'code',None),'reserved_stock_unavailable')
            self.assertEqual(conn.total_changes,before);conn.rollback()

    def test_duplicate_official_status_is_rejected_and_global_sequence_is_preserved(self):
        with self.assertRaisesRegex(ValueError,'exact_33_order_scope_required'):evidence_fixture(duplicate_status=True)
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for table in (orders.OBSERVATIONS_TABLE,orders.STATUS_OBSERVATIONS_TABLE):
                conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',(table,100000))
            self.plan=historical.prepare_historical_handoffs(conn,runtime_dir=self.runtime.runtime_dir,evidence=self.evidence,
                allocation_document_id=self.allocation_id,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')
            conn.commit()
        self.assertEqual(self.plan['orders'][0]['order_sequence'],100001)
        self.assertEqual(self.apply()['physical_debits'],32)

    def test_finite_long_tail_full_exit_and_repeating_wac_fit_schema(self):
        capital='9000.'+'0'*55+'123456789'
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(f"UPDATE {BALANCES_TABLE} SET quantity=1,capital_rub=?,wac_rub=? WHERE pool='FBS' AND nm_id=101",(capital,capital))
            manifest=self.plan['manifest'];order=dict(order_id=9999,quantity=1,nm_id=101,facility_id='synthetic-moscow')
            wac,_=lifecycle._current_handoff_wac(conn,manifest=manifest,order=order)
            self.assertEqual(wac,Decimal(capital))
            event=lifecycle._append_event(conn,manifest=manifest,order=order,episode_sequence=1,event_type='handoff_debit',state='fulfilled',
                supplier_status='complete',wb_status='sorted',status_digest='synthetic',source_revision='synthetic',source_observed_at=STAMP,
                wac=wac,physical_delta=-1,occurred_at=STAMP,evidence={'synthetic':True})
            lifecycle._apply_exact_physical_delta(conn,manifest=manifest,order=order,event_id=event['event_id'],quantity_delta=-1,wac=wac,
                occurred_at=STAMP,source_business_date='2026-10-29')
            self.assertEqual(tuple(conn.execute(f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} WHERE pool='FBS' AND nm_id=101").fetchone()),(0,'0',None))
            conn.rollback()
        for capital,q in zip(CAPITALS,QUANTITIES):
            self.assertLessEqual(len(str(lifecycle._stored_wac_ratio(Decimal(capital),q))),80)

    def test_one_transaction_ordinary27_32_handoffs_and_exact29_return(self):
        runtime=SimpleNamespace(runtime_dir=self.runtime.runtime_dir,db_path=self.pre27)
        with warehouse_functional_job_lock(runtime.runtime_dir) as owner, warehouse_functional_write_lock(runtime.runtime_dir), writer_lock(runtime.runtime_dir):
            # Rehearse a complete rollback after the final monetary boundary.
            for rollback in (True,False):
                with closing(_connect(runtime.db_path)) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    allocation=apply_ordinary_reallocation(conn,'unit27','2026-10-27',source='FBO',destination='FBS',quantities=dict(zip(range(101,105),QUANTITIES)))
                    plan=historical.prepare_historical_handoffs(conn,runtime_dir=runtime.runtime_dir,evidence=self.evidence,
                        allocation_document_id=allocation,original_document_id='original07',posted_at=STAMP,approval_reference='local-owner')
                    historical.apply_historical_handoffs(conn,plan,runtime=runtime,owner_token=owner['owner_token'])
                    quantities={int(row[0]):int(row[1]) for row in conn.execute(f"SELECT nm_id,quantity FROM {BALANCES_TABLE} WHERE pool='FBS'")}
                    returned=apply_ordinary_reallocation(conn,'unit29','2026-10-29',source='FBS',destination='FBO',quantities=quantities)
                    self.assertTrue(conn.in_transaction)
                    for row in conn.execute(f'SELECT * FROM {BALANCES_TABLE}'):
                        if row['pool']=='FBS':self.assertEqual((row['quantity'],row['capital_rub'],row['wac_rub']),(0,'0',None))
                    self.assertEqual(sum(quantities.values()),5368)
                    for nm,c,sent in zip(range(101,105),CAPITALS,(8,12,8,4)):
                        balance=conn.execute(f"SELECT capital_rub FROM {BALANCES_TABLE} WHERE nm_id=? AND pool='FBO'",(nm,)).fetchone()
                        with localcontext() as context:
                            context.prec=160
                            self.assertEqual(Decimal(balance[0])+Decimal(plan['sources'][str(nm)]['wac_rub'])*sent,Decimal(c))
                    if rollback:
                        conn.rollback()
                        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {lifecycle.EVENTS_TABLE}').fetchone()[0],0)
                        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE}').fetchone()[0],1)
                    else:
                        conn.commit()
                        # Existing ordinary observer sees the actual 33 source
                        # statuses after source stock has completely left FBS.
                        conn.execute('BEGIN IMMEDIATE')
                        before=conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0]
                        replay=lifecycle.process_post_t_fbs_lifecycle(conn,occurred_at=STAMP,schema_ready=True)
                        after=conn.execute(f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE operation_type='fbs_physical_handoff'").fetchone()[0]
                        self.assertEqual((before,after),(32,32));self.assertEqual(replay['summary']['fulfilled'],0)
                        self.assertEqual(conn.execute(f'SELECT state FROM {lifecycle.CURRENT_TABLE} WHERE order_id=1001').fetchone()[0],'fulfilled_reconciliation')
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {lifecycle.RECONCILIATION_TABLE} WHERE state='open'").fetchone()[0],1)
                        self.assertEqual(historical.read_historical_handoff_plan(conn),plan)
                        lifecycle.process_post_t_fbs_lifecycle(conn,occurred_at=STAMP,schema_ready=True)
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {lifecycle.RECONCILIATION_TABLE} WHERE state='open'").fetchone()[0],1)
                        conn.rollback()


if __name__=='__main__':unittest.main()
