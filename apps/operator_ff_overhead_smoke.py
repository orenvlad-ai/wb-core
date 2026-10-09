#!/usr/bin/env python3
"""Disposable source acceptance, native drain and actual dated book/ready proofs."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.ready_publication_smoke import seed, save, make_plan, clock
from apps.fbs_document_cost_smoke import capture
from apps.fbs_inventory_presentation_smoke import retained
from apps.shared_sku_cost_smoke import wb
from apps.russian_payment_orders_smoke import _fixture, _render_pdf
from packages.application import fbs_accounting_runtime as accounting
from packages.application import operator_ff_overhead as operations
from packages.application.ff_pool_documents import FfPoolDocumentService, REQUESTS_TABLE, DOCUMENTS_TABLE, TARGETED_RECALC_QUEUE_TABLE
from packages.application.ff_pool_foundation import FEATURE_EPOCHS_TABLE, FACILITIES_TABLE, BALANCES_TABLE
from packages.application.ff_pool_surfaces import FfPoolSurface, FfPoolSurfaceError
from packages.application.fbs_snapshot_cost_sources import _documents
from packages.application.ready_publication import readonly, capture_material, check_material
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='operator-ff-confirmation-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = seed(self.root)
        self.now = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)
        self.service = FfPoolDocumentService(db_path=self.runtime.db_path, runtime_dir=self.root, resume=False,
                                            timestamp_factory=self.stamp)
        self.surface = FfPoolSurface(db_path=self.runtime.db_path, runtime_dir=self.root, timestamp_factory=self.stamp)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(1,1,0,'fixture',?,'{{}}')", (self.stamp(),))
            conn.execute(f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at) VALUES('A','A','Синтетический FF',1,'Europe/Moscow',?,?)", (self.stamp(),self.stamp()))
            for pool in ('FBS','FBO'):
                conn.execute(f"INSERT INTO {BALANCES_TABLE}(facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at) VALUES('A',?,1,1,2,'20','10','fixture',?)", (pool,self.stamp()))
        self.payload = dict(request_id='confirm-fixture',facility_id='A',scope='both',category='storage',amount_rub='90',comment='')

    def stamp(self):
        return self.now.isoformat().replace('+00:00','Z')

    def preview(self, **fields):
        return self.surface.accept_pool_overhead_preview({**self.payload, **fields},actor='fixture-operator')

    def drain(self):
        with warehouse_functional_job_lock(self.root):
            return operations.drain(self.runtime.db_path,self.root,timestamp_factory=self.stamp)

    def reconcile(self):
        with warehouse_functional_job_lock(self.root):
            return operations.reconcile(self.runtime,now=self.now)

    def native_terminal(self, identity):
        with sqlite3.connect(self.runtime.db_path) as conn:
            return conn.execute(f"""SELECT r.state,recovery.lifecycle_state
                FROM {REQUESTS_TABLE} r JOIN sheet_vitrina_v1_recovery_operations recovery
                ON recovery.operation_id=r.recovery_operation_id WHERE r.request_id=?""", (identity,)).fetchone()

    def counts(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            return tuple(conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in (operations.TABLE,DOCUMENTS_TABLE))

    def prepare(self, *, opening=False, quantity=1000):
        with operations.readonly(self.runtime.db_path) as conn:
            docs = _documents(conn)
        image = capture(self.now.date().isoformat(), docs=docs, rows=[('A',1,quantity,'100')], fbo=[('A',1,2000,'200')])
        image['captured_at'] = self.stamp()
        image['quantity_snapshot']['captured_at'] = self.stamp()
        from packages.application.fbs_snapshot_cost import fingerprint
        image['source_digest'] = fingerprint({k:v for k,v in image.items() if k != 'source_digest'})
        component = wb(self.now.date().isoformat())
        with patch.object(accounting,'capture_current',return_value=image), \
             patch.object(accounting,'capture_wb_component',return_value=component), \
             patch.object(accounting,'capture_retained_stages',return_value=retained(component)):
            return accounting.prepare(self.root,now=self.now,opening=opening)

    def publish(self, prepared, *, previous=None):
        day = self.now.date().isoformat()
        previous = previous or (self.now.date()-timedelta(days=1)).isoformat()
        return save(self.runtime,make_plan(previous,day),prepared=prepared,now=self.now)

    def open_book(self):
        self.publish(self.prepare(opening=True))

    def mark_native_publications(self):
        # Isolated downstream fixtures, not forged accounting completion: the
        # actual book/ready receipt and allocation still have to pass readback.
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET status='complete',economics_status='complete',finance_status='complete'")

    def test_preview_is_not_journal_acceptance_and_failed_allocation_does_not_block_source(self):
        from packages.application.fbs_overhead_presentation import OverheadAccountingView
        preview = self.preview()
        self.assertIsNone(preview['acceptance'])
        self.assertEqual(operations.journal(self.runtime.db_path)['total'],0)
        unavailable = {'allocation_status':'unavailable','allocation_label':'Unavailable','lines':[]}
        with patch.object(OverheadAccountingView,'resolve',return_value=unavailable), \
             patch.object(FfPoolDocumentService,'__init__',side_effect=AssertionError('confirmation constructs a service')), \
             patch.object(operations,'drain',side_effect=AssertionError('confirmation starts processing')):
            self.assertTrue(self.surface.request_status(preview['request_id'])['confirm_allowed'])
            receipt = self.surface.confirm_document(preview['request_id'])['acceptance']
        self.assertTrue(receipt['durable_saved'])
        self.assertEqual(receipt['state'],'accepted')
        self.assertIsNone(receipt['document'])
        self.assertEqual(self.counts(),(1,0))
        self.assertEqual(operations.journal(self.runtime.db_path)['items'][0]['operation_id'],receipt['operation_id'])

    def test_busy_cycle_does_not_block_confirmation_and_repeat_after_midnight_keeps_source(self):
        preview = self.preview()
        with warehouse_functional_job_lock(self.root):
            first = self.surface.confirm_document(preview['request_id'])['acceptance']
        self.now += timedelta(days=1)
        repeated = self.surface.confirm_document('confirm-fixture')['acceptance']
        self.assertEqual(first,repeated)
        self.assertEqual(repeated['business_date'],'2026-09-08')
        self.assertEqual(self.counts(),(1,0))
        # Reload/unknown HTTP response needs only saved client ID and query-only read.
        before = self.runtime.db_path.read_bytes()
        with patch.object(FfPoolDocumentService,'__init__',side_effect=AssertionError('GET bootstrap')):
            self.assertEqual(operations.read_acceptance(self.runtime.db_path,'confirm-fixture'),first)
            operations.journal(self.runtime.db_path)
        self.assertEqual(before,self.runtime.db_path.read_bytes())

    def test_confirmation_recovery_never_capture_candidate_and_preview_never_resets_other_request(self):
        first=self.preview(request_id='other-processing-source')
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='processing' WHERE request_id=?",(first['request_id'],))
        with patch('packages.application.ff_pool_documents.ensure_ff_pool_document_schema',side_effect=AssertionError('preview bootstrap')), \
             patch('packages.application.fbs_overhead_presentation.capture_current',side_effect=RuntimeError('derived capture unavailable')), \
             patch('packages.application.fbs_overhead_presentation.OverheadAccountingView.resolve',side_effect=AssertionError('source upload preview')):
            second=self.preview(request_id='new-expense-source',amount_rub='91')
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f"SELECT state FROM {REQUESTS_TABLE} WHERE request_id=?",(first['request_id'],)).fetchone()[0],'processing')
        with patch('packages.application.fbs_overhead_presentation.capture_current',side_effect=RuntimeError('derived capture unavailable')), \
             patch('packages.application.fbs_overhead_presentation.OverheadAccountingView.resolve',side_effect=AssertionError('receipt preview')):
            receipt=self.surface.confirm_document(second['request_id'])['acceptance']
            self.assertTrue(receipt['durable_saved'])
            self.assertEqual(self.surface.request_status(second['request_id'])['acceptance'],receipt)
            self.assertEqual(operations.journal(self.runtime.db_path)['total'],1)

    def test_stale_preview_rejected_inside_acceptance_transaction(self):
        preview = self.preview()
        def crossing():
            return (self.now+timedelta(days=1)).isoformat().replace('+00:00','Z')
        with self.assertRaisesRegex(ValueError,'Дата'):
            operations.confirm_source(self.runtime.db_path,preview['request_id'],now=self.stamp(),
                current_day='2026-09-08',runtime_dir=self.root,timestamp_factory=crossing)
        self.assertEqual(self.counts(),(0,0))

    def test_invalid_pdf_is_not_confirmable_and_equivalent_pdf_is_one_source(self):
        invalid = _render_pdf(_fixture('wb_bank_0401060.txt').replace('Проведено','Черновик'),title='invalid-source',x_offset=0)
        # Eligibility is also checked by native confirmation, regardless of UI.
        good = _render_pdf(_fixture('wb_bank_0401060.txt'),title='source-a',x_offset=0)
        equivalent = _render_pdf(_fixture('wb_bank_0401060_equivalent_layout.txt'),title='source-b',x_offset=24)
        a = self.surface.accept_pool_overhead_preview({**self.payload,'request_id':'pdf-confirm-a','amount_rub':''},actor='fixture',source_bytes=good,filename='a.pdf',content_type='application/pdf')
        self.surface.confirm_document(a['request_id'])
        b = self.surface.accept_pool_overhead_preview({**self.payload,'request_id':'pdf-confirm-b','amount_rub':''},actor='fixture',source_bytes=equivalent,filename='b.pdf',content_type='application/pdf')
        self.assertTrue(b['payment_duplicate'])
        self.assertEqual(a['request_id'],b['request_id'])
        self.assertEqual(self.surface.confirm_document('pdf-confirm-b')['acceptance']['request_id'],a['request_id'])
        self.assertEqual(self.counts(),(1,0))
        # Existing parser regression checks cover unsupported/nonexecuted/RUB;
        # verify final confirmation cannot bypass a tampered primary source.
        with sqlite3.connect(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                conn.execute(f"UPDATE {REQUESTS_TABLE} SET source_file_blob=? WHERE request_id=?",(invalid,a['request_id']))

    def test_confirmation_insert_failure_is_not_green_and_owned_native_lock_is_single_flight(self):
        import fcntl
        preview=self.preview()
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"CREATE TRIGGER reject_confirmation BEFORE INSERT ON {operations.TABLE} BEGIN SELECT RAISE(ABORT,'fixture admission interruption'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.surface.confirm_document(preview['request_id'])
        self.assertIsNone(operations.read_acceptance(self.runtime.db_path,preview['request_id']))
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute('DROP TRIGGER reject_confirmation')
        self.surface.confirm_document(preview['request_id'])
        with (self.root/'.ff-pool-document-posting.lock').open('a+b') as handle:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
            self.assertEqual(self.drain()['status'],'busy')
            self.assertEqual(self.counts(),(1,0))
        self.drain()
        self.assertEqual(self.counts(),(1,1))

    def test_native_post_exception_after_commit_is_recovered_without_double_expense(self):
        preview = self.preview()
        self.surface.confirm_document(preview['request_id'])
        original = FfPoolDocumentService._post_once_under_writer_lock
        def crash(service,identity,**kwargs):
            original(service,identity,**kwargs)
            raise RuntimeError('simulated process loss after native commit')
        with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',crash):
            self.drain()
        failed = operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(failed['state'],'delayed')
        self.assertEqual(failed['processing_receipt']['error_code'],'RuntimeError')
        self.assertTrue(failed['document']['posted'])
        self.drain()
        self.drain()
        self.assertEqual(self.counts(),(1,1))
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT sum(cast(amount_rub AS REAL)) FROM sheet_vitrina_v1_ff_pool_document_expense_lines').fetchone()[0],90)

    def test_real_native_source_and_published_daily_allocation_all_scopes(self):
        for scope in ('FBS','FBO','both'):
            with self.subTest(scope=scope):
                if scope != 'FBS':
                    self.tearDownFixture()
                    self.setUp()
                self.open_book()
                preview = self.preview(scope=scope)
                receipt = self.surface.confirm_document(preview['request_id'])['acceptance']
                self.assertEqual(receipt['state'],'accepted')
                self.drain()
                self.mark_native_publications()
                # Native queue flags cannot substitute for a canonical publication.
                self.assertEqual(self.reconcile()['processed_count'],0)
                self.publish(self.prepare())
                actual_receipt=accounting.current_publication_receipt
                with patch.object(accounting,'current_publication_receipt',side_effect=lambda *a,**kw: {
                        **actual_receipt(*a,**kw),'accounting_version':'another-book-version'}):
                    self.assertEqual(self.reconcile()['processed_count'],0)
                self.assertEqual(self.reconcile()['processed_count'],1)
                final = operations.read_acceptance(self.runtime.db_path,receipt['operation_id'])
                allocation = final['processing_receipt']['canonical_allocation']
                self.assertEqual(final['state'],'completed')
                self.assertEqual(self.native_terminal(receipt['request_id']),('complete','retained'))
                self.assertEqual(final['processing_receipt']['native_state'],'complete')
                self.assertEqual(final['processing_receipt']['native_recovery']['lifecycle'],'retained')
                self.assertEqual(self.reconcile()['processed_count'],0)
                self.drain()
                self.assertEqual(self.counts(),(1,1))
                # Native pool shares used 2+2 units. Actual accounting uses 1000+2000.
                self.assertEqual(allocation['denominator_quantity'],3000 if scope=='both' else (1000 if scope=='FBS' else 2000))
                self.assertEqual(allocation['allocation_total_rub'],'90')
                self.assertEqual(allocation['accounting_version'],final['processing_receipt']['accounting_publication']['accounting_version'])
                if scope=='both':
                    self.assertEqual(allocation['pool_allocations_rub'],{'FBS':'30','FBO':'60'})

    def test_terminal_recovery_restart_after_retain_or_native_complete_does_not_repost(self):
        from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
        from packages.application.warehouse_functional_lock import WarehouseJobOwnershipError
        with self.assertRaises(WarehouseJobOwnershipError):
            operations.reconcile(self.runtime,now=self.now)
        for boundary in ('retain','native_complete'):
            with self.subTest(boundary=boundary):
                if boundary != 'retain':
                    self.tearDownFixture()
                    self.setUp()
                self.open_book()
                preview=self.preview()
                self.surface.confirm_document(preview['request_id'])
                self.drain()
                self.mark_native_publications()
                self.publish(self.prepare())
                target=WarehouseRecoveryRegistry if boundary=='retain' else FfPoolDocumentService
                method='retain' if boundary=='retain' else '_finalize_posted'
                original=getattr(target,method)
                def crash(instance,*args,**kwargs):
                    original(instance,*args,**kwargs)
                    raise RuntimeError('simulated process loss after '+boundary)
                with patch.object(target,method,crash):
                    self.assertEqual(self.reconcile()['processed_count'],0)
                receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
                self.assertEqual(receipt['state'],'delayed')
                self.assertEqual(receipt['processing_receipt']['error_code'],'RuntimeError')
                self.assertEqual(self.native_terminal(preview['request_id']),
                    ('replay' if boundary=='retain' else 'complete','retained'))
                # A fresh cycle/service retries final readback/retention only.
                with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',
                                  side_effect=AssertionError('terminal recovery reposted source')):
                    self.drain()
                    self.assertEqual(self.reconcile()['processed_count'],1)
                    self.assertEqual(self.reconcile()['processed_count'],0)
                self.assertEqual(self.native_terminal(preview['request_id']),('complete','retained'))
                self.assertEqual(self.counts(),(1,1))
                with sqlite3.connect(self.runtime.db_path) as conn:
                    self.assertEqual(conn.execute(f'SELECT count(*) FROM {TARGETED_RECALC_QUEUE_TABLE}').fetchone()[0],1)
                    self.assertEqual(conn.execute('SELECT sum(cast(amount_rub AS REAL)) FROM sheet_vitrina_v1_ff_pool_document_expense_lines').fetchone()[0],90)

    def test_explicit_disabled_writer_requires_attention_without_losing_source(self):
        preview=self.preview()
        self.surface.confirm_document(preview['request_id'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(2,0,0,'writer-disabled',?,'{{}}')",(self.stamp(),))
        self.drain()
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(receipt['state'],'needs_attention')
        self.assertEqual(receipt['reason_code'],'posting_blocked')
        self.assertEqual(receipt['processing_receipt']['error_code'],'feature_writer_disabled')
        self.assertEqual(self.counts(),(1,0))

    def tearDownFixture(self):
        self.tmp.cleanup()

    def test_prior_day_pending_and_post_watermark_race_hold_closure_then_next_cycle_publishes_original_day(self):
        self.open_book()
        preview = self.preview()
        # Capture prior-day closing candidate before final confirmation (cutoff).
        self.now += timedelta(days=1)
        prepared = self.prepare(quantity=800)
        self.now -= timedelta(days=1)
        first = self.surface.confirm_document(preview['request_id'])['acceptance']
        self.now += timedelta(days=1)
        with self.assertRaisesRegex(ValueError,'confirmed_overhead_day_pending'):
            self.publish(prepared)
        self.assertEqual(accounting.load(self.root)[0]['state']['periods']['2026-09-08']['status'],'open')
        with self.assertRaisesRegex(ValueError,'confirmed_overhead_day_pending'):
            self.prepare(quantity=800)
        # Attention cases retain the financial obligation and still hold closure.
        operations._update(self.runtime.db_path,first['request_id'],'needs_attention','posting_blocked')
        with self.assertRaisesRegex(ValueError,'confirmed_overhead_day_pending'):
            self.prepare(quantity=800)
        operations._update(self.runtime.db_path,first['request_id'],'delayed','accounting_unavailable')
        self.drain()
        self.mark_native_publications()
        self.publish(self.prepare(quantity=800),previous='2026-09-08')
        self.assertEqual(self.reconcile()['processed_count'],1)
        book,_ = accounting.load(self.root)
        prior=book['state']['periods']['2026-09-08']
        self.assertEqual(prior['status'],'closed')
        self.assertIn(operations.read_acceptance(self.runtime.db_path,first['request_id'])['document']['document_id'],prior['applied_documents'])
        self.assertEqual(operations.read_acceptance(self.runtime.db_path,first['request_id'])['business_date'],'2026-09-08')
        self.assertEqual(book['state']['pending_documents'],[])

    def test_current_day_acceptance_after_cutoff_does_not_invalidate_current_publication(self):
        self.open_book()
        self.now += timedelta(minutes=5)
        preview=self.preview()
        prepared=self.prepare()
        # Only material source tables used by book source operands enter CAS.
        with readonly(self.runtime.db_path) as conn:
            inputs=capture_material(conn)
        self.surface.confirm_document(preview['request_id'])
        with readonly(self.runtime.db_path) as conn:
            check_material(conn,inputs)
        self.publish(prepared)
        self.assertEqual(operations.read_acceptance(self.runtime.db_path,preview['request_id'])['state'],'accepted')
        self.drain()
        self.mark_native_publications()
        self.publish(self.prepare())
        self.assertEqual(self.reconcile()['processed_count'],1)

    def pdf_preview(self, request_id, *, pdf=None, filename='receipt.pdf', **fields):
        pdf = pdf or _render_pdf(_fixture('wb_bank_0401060.txt'),title='prior-day-source',x_offset=0)
        return self.surface.accept_pool_overhead_preview({**self.payload,'request_id':request_id,'amount_rub':'',**fields},
            actor='fixture',source_bytes=pdf,filename=filename,content_type='application/pdf')

    def test_unconfirmed_pdf_prior_day_renewal_retains_evidence_and_only_current_day_cost(self):
        from packages.application.ff_pool_documents import OVERHEAD_PAYMENT_EVIDENCE_TABLE, OVERHEAD_PAYMENT_RENEWALS_TABLE
        self.open_book()
        pdf=_render_pdf(_fixture('wb_bank_0401060.txt'),title='prior-day-source',x_offset=0)
        old=self.pdf_preview('pdf-day1',pdf=pdf)
        with sqlite3.connect(self.runtime.db_path) as conn:
            evidence_before=conn.execute(f'SELECT * FROM {OVERHEAD_PAYMENT_EVIDENCE_TABLE}').fetchall()
            old_source=conn.execute(f'SELECT request_identity,source_revision,business_date,source_sha256,source_file_blob,request_payload_json FROM {REQUESTS_TABLE} WHERE request_id=?',(old['request_id'],)).fetchone()
        self.now += timedelta(days=1)
        regenerated=_render_pdf(_fixture('wb_bank_0401060.txt'),title='regenerated-bank-copy',x_offset=9)
        with patch('packages.application.ff_pool_documents._build_posting_plan',side_effect=AssertionError('HTTP builds allocation')) as build, \
             patch('packages.application.fbs_overhead_presentation.capture_current',side_effect=AssertionError('HTTP captures book')):
            today=self.pdf_preview('pdf-day2',pdf=regenerated,filename='renamed.pdf')
            build.assert_not_called()
        self.assertEqual(today['state'],'ready')
        self.assertEqual(today['preview']['summary']['allocation_label'],'Распределение будет выполнено после подтверждения в плановом цикле')
        self.assertEqual(today['error']['code'],'')
        self.assertNotEqual(today['request_id'],old['request_id'])
        self.assertEqual(today['business_date'],'2026-09-09')
        self.assertTrue(today['confirm_allowed'])
        for fields in ({'comment':'same-day-different'},{'category':'receiving'},{'scope':'FBS'}):
            with self.assertRaises(FfPoolSurfaceError) as raised:self.pdf_preview('same-day-fields',pdf=regenerated,**fields)
            self.assertEqual(raised.exception.code,'overhead_payment_fields_mismatch')
            self.assertEqual(raised.exception.details['request_id'],today['request_id'])
        self.assertEqual(today['source']['sha256'],old['source']['sha256'])
        self.assertEqual(today['source']['filename'],'receipt.pdf')
        self.assertEqual(today['preview']['summary']['payment_evidence'],old['preview']['summary']['payment_evidence'])
        stale=self.surface.request_status('pdf-day1')
        self.assertEqual(stale['superseded_by']['request_id'],today['request_id'])
        self.assertFalse(stale['confirm_allowed'])
        self.assertIsNone(stale['acceptance'])
        for identity in (old['request_id'],'pdf-day1'):
            with self.assertRaises(FfPoolSurfaceError) as raised:self.surface.confirm_document(identity)
            self.assertEqual(raised.exception.code,'overhead_preview_superseded')
        self.assertEqual(self.service.post(old['request_id'])['state'],'blocked')
        repeated=self.pdf_preview('pdf-day1',pdf=regenerated)
        self.assertEqual(repeated['request_id'],today['request_id'])
        self.assertTrue(repeated['payment_duplicate'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT * FROM {OVERHEAD_PAYMENT_EVIDENCE_TABLE}').fetchall(),evidence_before)
            self.assertEqual(conn.execute(f'SELECT request_identity,source_revision,business_date,source_sha256,source_file_blob,request_payload_json FROM {REQUESTS_TABLE} WHERE request_id=?',(old['request_id'],)).fetchone(),old_source)
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {OVERHEAD_PAYMENT_RENEWALS_TABLE}').fetchone()[0],1)
            for verb in ('UPDATE','DELETE'):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f'{verb} ' + ('FROM ' if verb=='DELETE' else '') + OVERHEAD_PAYMENT_RENEWALS_TABLE + (' SET actor=actor' if verb=='UPDATE' else ''))
        self.assertEqual(self.counts(),(0,0))
        receipt=self.surface.confirm_document(today['request_id'])['acceptance']
        self.assertEqual(receipt['business_date'],'2026-09-09')
        self.assertEqual(self.surface.request_status(today['request_id'])['preview']['summary']['allocation_label'],'Ожидает публикации в расчёте себестоимости')
        self.assertEqual(self.drain()['processed_count'],1)
        self.mark_native_publications()
        self.publish(self.prepare(quantity=800))
        self.assertEqual(self.reconcile()['processed_count'],1)
        book,_=accounting.load(self.root)
        doc=operations.read_acceptance(self.runtime.db_path,today['request_id'])['document']['document_id']
        self.assertNotIn(doc,book['state']['periods']['2026-09-08']['applied_documents'])
        self.assertIn(doc,book['state']['periods']['2026-09-09']['applied_documents'])
        self.now += timedelta(days=1)
        with sqlite3.connect(self.runtime.db_path) as conn:
            frozen=conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(today['request_id'],)).fetchone()
        self.assertEqual(self.pdf_preview('pdf-completed',pdf=pdf)['request_id'],today['request_id'])
        self.assertEqual(self.drain()['processed_count'],0)
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(today['request_id'],)).fetchone(),frozen)
        self.assertEqual(self.counts(),(1,1))

    def test_pdf_renewal_through_multipart_http_never_builds_allocation(self):
        from contextlib import closing
        from threading import Thread
        from urllib.request import Request, urlopen
        from apps.ff_pool_surfaces_http_smoke import _reserve_free_port
        from packages.adapters.registry_upload_http_entrypoint import (build_registry_upload_http_server, DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH,
            DEFAULT_UPLOAD_PATH, DEFAULT_SHEET_PLAN_PATH, DEFAULT_SHEET_STATUS_PATH, DEFAULT_SHEET_OPERATOR_UI_PATH)
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        pdf=_render_pdf(_fixture('wb_bank_0401060.txt'),title='http-source',x_offset=0)
        old=self.pdf_preview('http-original',pdf=pdf)
        self.now += timedelta(days=1)
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=_reserve_free_port(),runtime_dir=self.root,
            upload_path=DEFAULT_UPLOAD_PATH,sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH)
        entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=self.root,runtime=self.runtime,activated_at_factory=self.stamp)
        server=build_registry_upload_http_server(config,entrypoint=entrypoint)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        boundary='----wbc-renewal-fixture'
        fields={**self.payload,'request_id':'http-renewed','amount_rub':''}
        body=b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode() for key,value in fields.items()))
        body+=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="http-copy.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()+pdf+f'\r\n--{boundary}--\r\n'.encode())
        request=Request(f'http://127.0.0.1:{config.port}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}',data=body,method='POST',
            headers={'Content-Type':f'multipart/form-data; boundary={boundary}','X-WB-FF-Pool-CSRF':'1','Sec-Fetch-Site':'same-origin'})
        try:
            with patch('packages.application.ff_pool_documents._build_posting_plan',side_effect=AssertionError('HTTP builds allocation')) as build, \
                 patch('packages.application.fbs_overhead_presentation.capture_current',side_effect=AssertionError('HTTP captures book')) as capture:
                with closing(urlopen(request,timeout=10)) as response:today=json.load(response)
                build.assert_not_called();capture.assert_not_called()
            self.assertEqual(today['state'],'ready')
            self.assertEqual(today['business_date'],'2026-09-09')
            self.assertTrue(today['confirm_allowed'])
            self.assertNotEqual(today['request_id'],old['request_id'])
            self.assertEqual(self.counts(),(0,0))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_pdf_renewal_fields_epoch_and_inactive_facility_fail_closed(self):
        old=self.pdf_preview('guards-old')
        self.now += timedelta(days=1)
        for fields in ({'category':'receiving'},{'comment':'changed'},{'scope':'FBS'}):
            with self.assertRaises(FfPoolSurfaceError) as raised:self.pdf_preview('guards-new',**fields)
            self.assertEqual(raised.exception.code,'overhead_payment_fields_mismatch')
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(2,1,0,'changed',?,'{{}}')",(self.stamp(),))
        with self.assertRaises(FfPoolSurfaceError) as raised:self.pdf_preview('guards-new')
        self.assertEqual(raised.exception.code,'feature_epoch_changed')
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute(f"UPDATE {FACILITIES_TABLE} SET active=0 WHERE facility_id='A'")
        with self.assertRaises(FfPoolSurfaceError) as raised:self.pdf_preview('guards-new')
        self.assertEqual(raised.exception.code,'facility_not_active')
        self.assertEqual(self.counts(),(0,0))

    def test_pdf_accepted_and_legacy_posted_sources_are_frozen_on_reupload(self):
        from packages.application.ff_pool_documents import OVERHEAD_PAYMENT_RENEWALS_TABLE
        pdf=_render_pdf(_fixture('wb_bank_0401060.txt'),title='accepted-frozen',x_offset=0)
        old=self.pdf_preview('freeze-accepted',pdf=pdf)
        acceptance=self.surface.confirm_document(old['request_id'])['acceptance']
        with sqlite3.connect(self.runtime.db_path) as conn:
            frozen=conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(old['request_id'],)).fetchone()
        self.now += timedelta(days=1)
        self.assertEqual(self.pdf_preview('freeze-repeat',pdf=pdf)['acceptance'],acceptance)
        with self.assertRaises(FfPoolSurfaceError) as raised:self.pdf_preview('freeze-different',pdf=pdf,comment='changed')
        self.assertEqual(raised.exception.code,'overhead_payment_fields_mismatch')
        self.assertEqual(raised.exception.details['request_id'],old['request_id'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(old['request_id'],)).fetchone(),frozen)
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {OVERHEAD_PAYMENT_RENEWALS_TABLE}').fetchone()[0],0)
        self.drain()
        with sqlite3.connect(self.runtime.db_path) as conn:
            posted=conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(old['request_id'],)).fetchone()
        self.now += timedelta(days=1)
        self.assertEqual(self.pdf_preview('freeze-posted',pdf=pdf)['request_id'],old['request_id'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(old['request_id'],)).fetchone(),posted)
        self.assertEqual(self.counts(),(1,1))
        # A legacy native PDF document without a new confirmation also owns the
        # payment permanently. Exercise real native posting, not a forged state.
        legacy_pdf=_render_pdf(_fixture('wb_bank_0401060.txt').replace('№ 101','№ 102'),title='legacy-native',x_offset=0)
        legacy=self.pdf_preview('legacy-source',pdf=legacy_pdf)
        self.assertNotEqual(legacy['request_id'],old['request_id'])
        self.assertNotEqual(legacy['preview']['summary']['payment_evidence']['payment_fingerprint'],old['preview']['summary']['payment_evidence']['payment_fingerprint'])
        self.assertTrue(self.service._retry_pool_overhead_preview(legacy['request_id']))
        self.service.process_request(legacy['request_id'])
        native=self.service.post(legacy['request_id'],defer_replay=True)
        self.assertTrue(native['document'])
        self.now += timedelta(days=1)
        duplicate=self.pdf_preview('legacy-repeat',pdf=legacy_pdf)
        self.assertEqual(duplicate['request_id'],legacy['request_id'])
        self.assertIsNone(duplicate['acceptance'])
        self.assertFalse(duplicate['confirm_allowed'])
        self.assertEqual(self.counts(),(1,2))

    def test_pdf_multiday_chain_keeps_aliases_audit_and_domain_barrier(self):
        from packages.application.ff_pool_documents import OVERHEAD_PAYMENT_RENEWALS_TABLE, OVERHEAD_PAYMENT_EVIDENCE_TABLE, ALIASES_TABLE
        from packages.application.warehouse_domain_write_guard import ensure_warehouse_domain_write_guard_schema, EVENTS_TABLE
        pdf=_render_pdf(_fixture('wb_bank_0401060.txt'),title='chain',x_offset=0)
        first=self.pdf_preview('chain-original',pdf=pdf)
        self.now += timedelta(days=1)
        second=self.pdf_preview('chain-original',pdf=pdf)
        self.now += timedelta(days=1)
        third=self.pdf_preview('chain-today',pdf=pdf)
        self.assertEqual(third['business_date'],'2026-09-10')
        self.assertTrue(third['confirm_allowed'])
        self.assertEqual(self.pdf_preview('chain-original',pdf=pdf)['request_id'],third['request_id'])
        for old in (first,second):
            status=self.surface.request_status(old['request_id'])
            self.assertFalse(status['confirm_allowed'])
            self.assertEqual(status['superseded_by']['request_id'],third['request_id'])
            self.assertIsNone(status['acceptance'])
            with self.assertRaises(FfPoolSurfaceError):self.surface.confirm_document(old['request_id'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {OVERHEAD_PAYMENT_RENEWALS_TABLE}').fetchone()[0],2)
            self.assertEqual(conn.execute(f'SELECT request_id FROM {ALIASES_TABLE} WHERE client_request_id=?',('chain-original',)).fetchone()[0],first['request_id'])
            self.assertEqual(conn.execute(f'SELECT request_id FROM {OVERHEAD_PAYMENT_EVIDENCE_TABLE}').fetchone()[0],first['request_id'])
            ensure_warehouse_domain_write_guard_schema(conn)
            conn.execute(f"INSERT INTO {EVENTS_TABLE}(epoch_id,phase,manifest_digest,deployed_sha,event_at,actor) VALUES('fixture-guard','held',?,?,?,'fixture')",('sha256:'+'1'*64,'1'*40,self.stamp()))
        self.now += timedelta(days=1)
        with self.assertRaises(sqlite3.IntegrityError) as raised:self.pdf_preview('chain-blocked',pdf=pdf)
        self.assertIn('warehouse domain write barrier',str(raised.exception))
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {OVERHEAD_PAYMENT_RENEWALS_TABLE}').fetchone()[0],2)
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {REQUESTS_TABLE}').fetchone()[0],3)
        self.assertEqual(self.counts(),(0,0))

    def test_pdf_two_uploads_and_old_new_confirmation_race_have_one_effect(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        pdf=_render_pdf(_fixture('wb_bank_0401060.txt'),title='race',x_offset=0)
        old=self.pdf_preview('race-old',pdf=pdf)
        self.now += timedelta(days=1)
        barrier=Barrier(3)
        def upload(client):
            barrier.wait()
            return self.pdf_preview(client,pdf=pdf)
        def confirm_old():
            barrier.wait()
            try:return self.surface.confirm_document('race-old')
            except FfPoolSurfaceError as error:return error.code
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(upload,'race-upload-1'),pool.submit(upload,'race-upload-2'),pool.submit(confirm_old)]
            first,second,old_result=[future.result() for future in futures]
        self.assertEqual(first['request_id'],second['request_id'])
        self.assertNotEqual(first['request_id'],old['request_id'])
        self.assertIn(old_result,('overhead_unconfirmed_payment_prior_day','overhead_preview_superseded'))
        barrier=Barrier(3)
        def confirm_new():
            barrier.wait()
            return self.surface.confirm_document(first['request_id'])
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(confirm_new),pool.submit(confirm_new),pool.submit(upload,'race-upload-3')]
            a,b,repeat=[future.result() for future in futures]
        self.assertEqual(a['acceptance']['operation_id'],b['acceptance']['operation_id'])
        self.assertEqual(repeat['request_id'],first['request_id'])
        self.assertEqual(self.counts(),(1,0))
        self.drain();self.drain()
        self.assertEqual(self.counts(),(1,1))

    def test_journal_bounds_missing_schema_and_inherited_section_permissions(self):
        from packages.adapters.registry_upload_http_entrypoint import _user_can_access_path, _required_section_for_path
        route='/v1/sheet-vitrina-v1/operations'
        self.assertEqual(_required_section_for_path(route),'supply')
        self.assertFalse(_user_can_access_path({'role':'operator','allowed_sections':['vitrina']},route))
        self.assertTrue(_user_can_access_path({'role':'operator','allowed_sections':['supply']},route))
        for page,limit in ((0,25),(1,101),(True,25)):
            with self.assertRaises(ValueError): operations.journal(self.runtime.db_path,page=page,limit=limit)
        bare=self.root/'bare.sqlite3'
        with sqlite3.connect(bare): pass
        before=bare.read_bytes()
        self.assertEqual(operations.journal(bare)['total'],0)
        self.assertIsNone(operations.read_acceptance(bare,'absent'))
        self.assertEqual(before,bare.read_bytes())


class VersionedMaterialTests(unittest.TestCase):
    def test_owned_midnight_crash_then_real_functional_apply(self):
        from apps.warehouse_fbs_material_rematerialization_smoke import _seed, FACILITY_ID
        from packages.application.warehouse_functional import WarehouseFunctionalBlock
        from packages.application.warehouse_functional_lock import WarehouseJobOwnershipError
        with TemporaryDirectory(prefix='operator-versioned-material-') as d:
            r=_seed(Path(d),mixed=False)
            from packages.application.fulfillment_services import _ensure_schema
            with sqlite3.connect(r.db_path) as conn:
                conn.row_factory=sqlite3.Row
                _ensure_schema(conn)
                from packages.application.canonical_cost_engine import ensure_canonical_cost_schema as ensure_cost_schema
                ensure_cost_schema(conn)
                conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture-baseline',1,'2026-08-01','fixture-shipment','2026-08-01','1974',2,'10',0,0,'sha256:fixture-baseline','{}',1,'2026-08-01T00:00:00Z',NULL)")
            with sqlite3.connect(r.db_path) as conn:
                conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('fixture-cutover','sha256:fixture-cutover','0'*40,'2026-08-26T12:00:00Z','2026-08-26',1,'source-version','sha256:aggregate','sha256:detail',0,'sha256:watermark','sha256:mapping','sha256:origins','sha256:control','sha256:nontarget','fixture-opening','sha256:source','2026-08-26T12:00:00Z','{}'))
            with sqlite3.connect(r.db_path) as conn:
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(version_id,cutover_id,version_kind,effective_at,business_effective_date,published_at,status,plan_fingerprint,local_source_digest,source_watermarks_json,created_at) SELECT 'fixture-opening-version',cutover_id,'functional_cutover',effective_at,business_effective_date,published_at,status,'sha256:fixture-opening-plan',local_source_digest,source_watermarks_json,created_at FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id='whfv_incident_source'")
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json) SELECT 'fixture-opening-version',warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id='whfv_incident_source'")
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,items_json,created_at) SELECT 'fixture-opening-snapshot','fixture-opening-version',fetched_at,snapshot_date,requested_nm_ids_json,pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,items_json,created_at FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id='whfv_incident_source'")
            with sqlite3.connect(r.db_path) as conn:
                from datetime import date,timedelta
                day=date(2026,7,1)
                while day<date(2026,8,26):
                    conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_daily_state VALUES(?,101,'WB','0','0','0','0','0','0',NULL,NULL,'0','0','0','fixture','{}','2026-08-26T12:00:00Z',?)",(day.isoformat(),'sha256:'+day.isoformat()))
                    conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES('warehouse_functional_cutover_v1',?,101,'0','20','0','fixture','{}',?,'2026-08-26T12:00:00Z')",(day.isoformat(),'sha256:wb-'+day.isoformat()))
                    day+=timedelta(days=1)
            t={'at':'2026-08-26T12:00:00Z'}
            stamp=lambda:t['at']
            service=FfPoolDocumentService(db_path=r.db_path,runtime_dir=r.runtime_dir,resume=False,timestamp_factory=stamp)
            surface=FfPoolSurface(db_path=r.db_path,runtime_dir=r.runtime_dir,timestamp_factory=stamp)
            preview=surface.accept_pool_overhead_preview(dict(request_id='material-overhead-fixture',facility_id=FACILITY_ID,
                scope='FBS',amount_rub='90',category='storage'),actor='fixture')
            surface.confirm_document(preview['request_id'])
            with self.assertRaises(WarehouseJobOwnershipError):
                operations.drain(r.db_path,r.runtime_dir,timestamp_factory=stamp)
            t['at']='2026-08-27T12:00:00Z'
            original=FfPoolDocumentService._post_once_under_writer_lock
            def crash(service,identity,**kwargs):
                original(service,identity,**kwargs)
                raise RuntimeError('crash before functional projection')
            with warehouse_functional_job_lock(r.runtime_dir), patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',crash):
                operations.drain(r.db_path,r.runtime_dir,timestamp_factory=stamp)
            receipt=operations.read_acceptance(r.db_path,preview['request_id'])
            self.assertTrue(receipt['document']['posted'])
            self.assertEqual(receipt['business_date'],'2026-08-26')
            with sqlite3.connect(r.db_path) as conn:
                self.assertEqual(conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active').fetchone()[0],'whfv_incident_source')
                self.assertEqual(conn.execute(f'SELECT effective_date FROM {TARGETED_RECALC_QUEUE_TABLE}').fetchone()[0],'2026-08-26')
            with warehouse_functional_job_lock(r.runtime_dir):
                operations.drain(r.db_path,r.runtime_dir,timestamp_factory=stamp)
                block=WarehouseFunctionalBlock(runtime=r,timestamp_factory=stamp)
                payload=block._last_good_wb_payload()
                payload['snapshot_date']='2026-08-27';payload['data']['fetched_at']=stamp()
                plan=block._build_plan(kind='hourly_wb_sync',wb_payload=payload)
                result=block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])
            self.assertEqual(result['status'],'ready')
            with sqlite3.connect(r.db_path) as conn:
                total=conn.execute("SELECT sum(cast(b.capital_rub AS REAL)) FROM sheet_vitrina_v1_warehouse_functional_balances b JOIN sheet_vitrina_v1_warehouse_functional_active a ON a.version_id=b.version_id WHERE b.warehouse_key='ff'").fetchone()[0]
                self.assertEqual(total,19530+420+90)
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {DOCUMENTS_TABLE}').fetchone()[0],1)
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {TARGETED_RECALC_QUEUE_TABLE}').fetchone()[0],1)
            # Native T1 verifies exactly the mutations that really committed.
            service._verify_posted_readback(preview['request_id'])


class NativeCompletionTests(unittest.TestCase):
    """Actual local HTTP owner and native source/book/Ready/Finance/retention.

    Only external collection and synthetic official input assembly are fixtures;
    the source posting, serialized functional publisher and all completion proofs
    execute production native adapters against disposable SQLite files.
    """
    setUp=AcceptanceTests.setUp
    stamp=AcceptanceTests.stamp
    preview=AcceptanceTests.preview
    drain=AcceptanceTests.drain
    prepare=AcceptanceTests.prepare
    publish=AcceptanceTests.publish
    open_book=AcceptanceTests.open_book
    native_terminal=AcceptanceTests.native_terminal
    counts=AcceptanceTests.counts

    def quantities(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            return conn.execute(f'SELECT facility_id,pool,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id').fetchall()

    def payment(self, *, weekly=True, pdf=None):
        from datetime import date
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        self.open_book()
        self.finance=WbFinanceWeeklyBlock(self.root,seller_id='canonical',now_factory=lambda:self.now)
        self.finance.ensure_schema()
        if weekly:
            self.finance.ingest_week(date(2026,9,7),date(2026,9,13),[_row(1,'2026-09-08',nm_id=1)])
        preview=(AcceptanceTests.pdf_preview(self,'native-pdf-source',pdf=pdf,scope='FBS') if pdf is not None
                 else self.preview(scope='FBS',amount_rub='61425'))
        self.identity=preview['request_id']
        self.surface.confirm_document(self.identity)
        return self.identity

    def functional_plan(self):
        from apps.operator_warehouse_documents_smoke import OperatorDocuments
        from packages.application.warehouse_functional import WarehouseFunctionalBlock
        captured={}
        def collect(block,plan,**kwargs):
            captured.update(block=block,plan=plan);return plan
        with patch.object(WarehouseFunctionalBlock,'apply_plan',collect):
            OperatorDocuments.functional_publish(self)
        return captured['block'],captured['plan']

    def old_tail(self, *, weekly=True, pdf=None):
        self.payment(weekly=weekly,pdf=pdf);self.drain()
        with warehouse_functional_job_lock(self.root):
            block,plan=self.functional_plan()
            self.functional=block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=self.root/'backups')
        self.publish(self.prepare())
        self.before=self.business_image()
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT status,economics_status,finance_status FROM {TARGETED_RECALC_QUEUE_TABLE}').fetchone(),('complete','',''))

    def business_image(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.row_factory=sqlite3.Row
            rows={table:[dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in (
                DOCUMENTS_TABLE,'sheet_vitrina_v1_ff_pool_document_expense_lines','sheet_vitrina_v1_ff_pool_document_lines')}
            rows['sources']=[tuple(r) for r in conn.execute(f'SELECT request_id,source_json,source_digest,accepted_at,actor FROM {operations.TABLE}')]
        book,_=accounting.load(self.root)
        rows['allocation']=deepcopy(book['state']['periods'][self.now.date().isoformat()]['document_valuation']['allocations'])
        return rows

    def proof_inputs(self):
        captured=operations.capture_pending_completion(self.runtime,finance_block=self.finance)
        receipt=self.finance.recalculate_stale_cost_weeks()
        version=accounting.load(self.root)[1]
        receipt.update(accounting_version=version,accounting_version_before=version,accounting_version_unchanged=True)
        return captured,receipt,dict(accounting_publication=accounting.current_publication_receipt(self.runtime,now=self.now))

    def complete(self,captured,receipt,economics):
        return operations.complete_current_cycle(self.runtime,captured,finance_block=self.finance,finance_receipt=receipt,economics_receipt=economics,now=self.now)

    def http_pass(self, *, fresh=False):
        from types import SimpleNamespace
        from contextlib import closing
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.application.warehouse_update_journal import WarehouseUpdateJournal
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.calculation_parameters import CalculationParametersBlock
        native={}
        def build():
            if fresh:
                native['block'],native['plan']=self.functional_plan();return native['plan']
            with closing(operations.readonly(self.runtime.db_path)) as conn:
                queues=list(conn.execute(f"SELECT queue_id FROM {TARGETED_RECALC_QUEUE_TABLE} WHERE status IN ('queued','running')"))
            self.assertEqual(queues,[]) # The actual old-tail plan exclusion.
            return dict(plan_fingerprint='no-new-queue',diff={})
        def apply(plan,**kwargs):
            if fresh:return native['block'].apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=self.root/'backups')
            return self.functional # No business source/functional replay for a completed old tail.
        entry=RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
        entry.runtime=self.runtime;entry.warehouse_update_journal=WarehouseUpdateJournal(db_path=self.runtime.db_path,runtime_dir=self.root)
        entry.wb_supplies_block=SimpleNamespace(sync_functional_sources=lambda **kw:{},collect_all_due_transit_costs=lambda:{},reconcile_functional_ff_state=lambda:{})
        entry.our_wb_cost_block=SimpleNamespace(materialize_wb_supply_cost_layers=lambda **kw:0)
        entry.warehouse_functional_block=SimpleNamespace(build_sync_plan=build,apply_plan=apply,record_failed_sync=lambda exc:None)
        entry.calculation_parameters_block=CalculationParametersBlock(runtime=self.runtime)
        entry.inventory_planning=SimpleNamespace(current=lambda:{})
        entry.wb_finance_weekly_block=self.finance;entry.activated_at_factory=self.stamp;entry.now_factory=lambda:self.now
        def refresh(*args,**kwargs):
            from packages.application.fbs_snapshot_cost import fingerprint
            prepared=self.prepare()
            if fingerprint(prepared[0])!=accounting.load(self.root)[1]: self.publish(prepared)
            p=accounting.current_publication_receipt(self.runtime,now=self.now)
            return dict(status='published',version=p['accounting_version'])
        actual_publication=accounting.current_publication_receipt
        with heavy_admitted(self.root,operation='cycle'),warehouse_functional_job_lock(self.root) as owner, \
             patch.object(accounting,'current_publication_receipt',side_effect=lambda runtime,now=None:actual_publication(runtime,now=now or self.now)), \
             patch.object(accounting,'refresh',side_effect=refresh), \
             patch('packages.application.operator_warehouse_documents.drain',return_value={'request_ids':[]}), \
             patch('packages.application.operator_warehouse_documents.reconcile',return_value={'processed_count':0}), \
             patch('packages.application.operator_fulfillment_services.reconcile',return_value={'processed_count':0}):
            return entry._handle_owned_warehouse_manual_sync_request(owner_token=owner['owner_token'])

    def test_actual_http_owned_fresh_native_publication_and_finance(self):
        self.payment()
        result=self.http_pass(fresh=True)
        self.assertEqual(result['status'],'success')
        self.assertEqual(self.native_terminal(self.identity),('complete','retained'))
        receipt=operations.read_acceptance(self.runtime.db_path,self.identity)
        self.assertEqual(receipt['state'],'completed');self.assertEqual(self.counts(),(1,1))
        proof=receipt['processing_receipt']['native_completion']
        self.assertEqual(proof['allocation']['allocation_total_rub'],'61425')
        self.assertEqual(proof['finance_publication']['applicable_weeks'],[['canonical','2026-09-07','2026-09-13']])
        self.assertTrue(proof['finance_publication']['receipt']['non_target_preserved'])

    def test_actual_http_owned_already_complete_blank_tail_and_repeat_no_repost(self):
        self.old_tail()
        with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',side_effect=AssertionError('duplicate target posting')):
            self.assertEqual(self.http_pass()['status'],'success')
            self.assertEqual(self.http_pass()['status'],'success')
        self.assertEqual(self.business_image(),self.before)
        self.assertEqual(operations.read_acceptance(self.runtime.db_path,self.identity)['state'],'completed')
        self.assertEqual(self.native_terminal(self.identity),('complete','retained'))

    def test_actual_no_applicable_raw_is_distinct_from_zero_stale_week_scope(self):
        self.old_tail(weekly=False)
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs();self.assertEqual(self.complete(*args)['processed_count'],1)
        proof=operations.read_acceptance(self.runtime.db_path,self.identity)['processing_receipt']['native_completion']
        self.assertEqual(proof['finance_publication']['applicable_weeks'],[])
        self.assertEqual(self.business_image(),self.before)

    def test_native_unicode_nm_scope_is_retained_and_marker_drift_rejected(self):
        import hashlib
        from datetime import date
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        for nm in ('\u00a01\u00a0','\u20031\u2003','\t1\n',1,'1'):
            for changed in (False,True):
                case=NativeCompletionTests();case.setUp()
                try:
                    case.old_tail()
                    row=_row(1,'2026-09-08',nm_id=1);row['nmId']=nm
                    native=case.finance.ingest_week(date(2026,9,7),date(2026,9,13),[row])
                    self.assertIn(native['status'],('loaded_preliminary','completed'))
                    original=FfPoolDocumentService._finalize_posted;injected=[]
                    def finalize(service,*args,**kwargs):
                        if changed:
                            with sqlite3.connect(case.runtime.db_path) as conn:
                                operation=json.loads(conn.execute('SELECT raw_json FROM wb_finance_weekly_raw_rows LIMIT 1').fetchone()[0])
                                operation['quantity']=2
                                payload=json.dumps(operation,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                                conn.execute('UPDATE wb_finance_weekly_raw_rows SET raw_json=?,row_hash=?',(payload,hashlib.sha256(payload.encode()).hexdigest()))
                            injected.append(True)
                        return original(service,*args,**kwargs)
                    with self.subTest(nm=nm,changed=changed),warehouse_functional_job_lock(case.root):
                        args=case.proof_inputs()
                        with patch.object(FfPoolDocumentService,'_finalize_posted',finalize):
                            if changed:
                                with self.assertRaises(ValueError):case.complete(*args)
                            else:self.assertEqual(case.complete(*args)['processed_count'],1)
                        receipt=operations.read_acceptance(case.runtime.db_path,case.identity)
                        weeks=receipt['processing_receipt']['native_completion']['finance_publication']['applicable_weeks']
                        self.assertEqual(weeks,[['canonical','2026-09-07','2026-09-13']])
                        if changed:
                            self.assertEqual(injected,[True])
                            self.assertEqual(receipt['state'],'processing')
                            self.assertEqual(case.native_terminal(case.identity),('posted','mutation_running'))
                            self.assertEqual(case.finance.plan_stale_cost_weeks()['stale_week_count'],1)
                        else:self.assertEqual(case.native_terminal(case.identity),('complete','retained'))
                    self.assertEqual(case.business_image(),case.before)
                finally:case.doCleanups()

    def test_native_plan_date_bounds_and_supported_operation_fallback(self):
        import hashlib
        from datetime import date
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        for operation_day,start,end,applicable in (('2026-09-14',7,13,True),('',14,20,True),('',7,13,False)):
            for changed in (False,True):
                case=NativeCompletionTests();case.setUp()
                try:
                    case.open_book()
                    for day in range(9,15):
                        case.now=case.now.replace(day=day);case.publish(case.prepare())
                    case.finance=WbFinanceWeeklyBlock(case.root,seller_id='canonical',now_factory=lambda:case.now)
                    case.finance.ensure_schema()
                    row=_row(1,operation_day,nm_id=1)
                    native=case.finance.ingest_week(date(2026,9,start),date(2026,9,end),[row])
                    preview=case.preview(scope='FBS',amount_rub='61425');case.identity=preview['request_id']
                    case.surface.confirm_document(case.identity);case.drain()
                    with warehouse_functional_job_lock(case.root):
                        block,plan=case.functional_plan()
                        case.functional=block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=case.root/'backups')
                    case.publish(case.prepare());before=case.business_image()
                    original=FfPoolDocumentService._finalize_posted;injected=[]
                    def finalize(service,*args,**kwargs):
                        if changed:
                            with sqlite3.connect(case.runtime.db_path) as conn:
                                operation=json.loads(conn.execute('SELECT raw_json FROM wb_finance_weekly_raw_rows LIMIT 1').fetchone()[0]);operation['quantity']=2
                                payload=json.dumps(operation,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                                conn.execute('UPDATE wb_finance_weekly_raw_rows SET raw_json=?,row_hash=?',(payload,hashlib.sha256(payload.encode()).hexdigest()))
                            injected.append(True)
                        return original(service,*args,**kwargs)
                    with self.subTest(operation_day=operation_day,start=start,changed=changed),warehouse_functional_job_lock(case.root):
                        args=case.proof_inputs()
                        self.assertEqual(case.finance.shared_cost_snapshot.effective_date,'2026-09-08')
                        with patch.object(FfPoolDocumentService,'_finalize_posted',finalize):
                            if changed:
                                with self.assertRaises(ValueError):case.complete(*args)
                            else:self.assertEqual(case.complete(*args)['processed_count'],1)
                        receipt=operations.read_acceptance(case.runtime.db_path,case.identity)
                        expected=[['canonical',f'2026-09-{start:02}',f'2026-09-{end:02}']] if applicable else []
                        self.assertEqual(receipt['processing_receipt']['native_completion']['finance_publication']['applicable_weeks'],expected)
                        if changed:
                            self.assertEqual(injected,[True]);self.assertEqual(receipt['state'],'processing')
                            self.assertEqual(case.native_terminal(case.identity),('posted','mutation_running'))
                        else:self.assertEqual(case.native_terminal(case.identity),('complete','retained'))
                    self.assertEqual(case.business_image(),before)
                finally:case.doCleanups()

    def test_false_foreign_economics_and_finance_never_ack_or_retain(self):
        self.old_tail()
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            for field,value in [('post_verify_stale_week_count',None),('post_verify_stale_week_count',True),('post_verify_stale_week_count',1),('source_advanced_after_apply',True),('fingerprint','sha256:foreign'),('accounting_version','foreign'),('accounting_version_unchanged',False),('target_image_digest','sha256:foreign'),('post_source_dependency',{})]:
                c,f,e=deepcopy(args);f[field]=value
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):self.complete(c,f,e)
            c,f,e=deepcopy(args);e['accounting_publication']['ready_digest']='sha256:foreign'
            with self.assertRaises(ValueError):self.complete(c,f,e)
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
        self.assertEqual(self.business_image(),self.before)

    def test_queue_exact_revision_nm_date_and_recovery_binding(self):
        self.old_tail()
        with warehouse_functional_job_lock(self.root):
            for column,value in [('source_revision','sha256:foreign'),('affected_nm_ids_json','[1,2]'),('effective_date','2026-09-07')]:
                with sqlite3.connect(self.runtime.db_path) as conn:
                    old=conn.execute(f'SELECT {column} FROM {TARGETED_RECALC_QUEUE_TABLE}').fetchone()[0]
                    conn.execute(f'UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET {column}=?',(value,))
                with self.subTest(column=column),self.assertRaises(ValueError):operations.capture_pending_completion(self.runtime,finance_block=self.finance)
                with sqlite3.connect(self.runtime.db_path) as conn:conn.execute(f'UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET {column}=?',(old,))
            with sqlite3.connect(self.runtime.db_path) as conn:
                conn.row_factory=sqlite3.Row;queue=dict(conn.execute(f'SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE}').fetchone())
                queue.update(queue_id='foreign',source_revision='foreign')
                conn.execute(f'INSERT INTO {TARGETED_RECALC_QUEUE_TABLE}({",".join(queue)}) VALUES({",".join("?" for _ in queue)})',list(queue.values()))
            with self.assertRaises(ValueError):operations.capture_pending_completion(self.runtime,finance_block=self.finance)
            with sqlite3.connect(self.runtime.db_path) as conn:
                conn.execute(f"DELETE FROM {TARGETED_RECALC_QUEUE_TABLE} WHERE queue_id='foreign'")
                conn.execute("UPDATE sheet_vitrina_v1_recovery_operations SET source_digest='foreign'")
            with self.assertRaises(ValueError):operations.capture_pending_completion(self.runtime,finance_block=self.finance)
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))

    def test_same_path_manifest_drift_after_finance_rejects(self):
        from dataclasses import replace
        from packages.application.storage_registry import atomic_write_manifest,manifest_payload,parse_manifest,_sha256
        self.old_tail()
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs();m=self.finance.store_registry.load();changed=replace(m,rollback_generation_id='foreign')
            payload=manifest_payload(changed,include_digest=False);payload['manifest_sha256']=_sha256(payload)
            atomic_write_manifest(self.finance.store_registry.manifest_path,parse_manifest(payload))
            with self.assertRaises(ValueError):self.complete(*args)
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))

    def test_actual_separate_connection_commit_during_native_readback_rejected(self):
        self.old_tail()
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs();original=self.finance._finance_source_dependency_fingerprint;injected=[]
            def dependency(*a,**kw):
                result=original(*a,**kw)
                if not injected:
                    injected.append(True)
                    with sqlite3.connect(self.runtime.db_path) as conn:conn.execute("UPDATE wb_finance_weekly_aggregates SET classifier_version='stale'")
                return result
            with patch.object(self.finance,'_finance_source_dependency_fingerprint',side_effect=dependency),self.assertRaises(ValueError):self.complete(*args)
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
        self.assertEqual(self.business_image(),self.before)

    def test_retention_lost_reply_next_native_pass_does_not_post(self):
        from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
        self.old_tail();original=WarehouseRecoveryRegistry.retain
        def lost(registry,*a,**kw):original(registry,*a,**kw);raise RuntimeError('lost retain reply')
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(WarehouseRecoveryRegistry,'retain',lost),self.assertRaises(RuntimeError):self.complete(*args)
            self.assertEqual(self.native_terminal(self.identity),('replay','retained'))
            with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',side_effect=AssertionError('duplicate')):
                self.assertEqual(self.complete(*self.proof_inputs())['processed_count'],1)
        self.assertEqual(self.business_image(),self.before)
        self.assertEqual(self.native_terminal(self.identity),('complete','retained'))

    def test_actual_after_marker_drift_cannot_finalize_or_bypass_cycle_proof(self):
        from dataclasses import replace
        from packages.application.storage_registry import atomic_write_manifest,manifest_payload,parse_manifest,_sha256
        original=FfPoolDocumentService._finalize_posted
        self.old_tail()
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs();manifest=self.finance.store_registry.load()
            def changed(service,*a,**kw):
                payload=manifest_payload(replace(manifest,rollback_generation_id='after-marker'),include_digest=False)
                payload['manifest_sha256']=_sha256(payload)
                atomic_write_manifest(self.finance.store_registry.manifest_path,parse_manifest(payload))
                return original(service,*a,**kw)
            with patch.object(FfPoolDocumentService,'_finalize_posted',changed),self.assertRaises(ValueError):self.complete(*args)
            self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
            self.assertEqual(operations.read_acceptance(self.runtime.db_path,self.identity)['state'],'processing')
            # Durable queue flags do not let constructor/resume skip the saved proof.
            self.service._finalize_posted(self.identity)
            self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
            self.assertEqual(self.business_image(),self.before)
            if manifest.implicit:self.finance.store_registry.manifest_path.unlink()
            else:atomic_write_manifest(self.finance.store_registry.manifest_path,manifest)
            self.assertEqual(self.complete(*self.proof_inputs())['processed_count'],1)

    def test_native_book_pointer_and_finance_target_races_after_marker_refuse(self):
        from contextlib import closing
        from packages.application.fbs_accounting_runtime import pack
        original=FfPoolDocumentService._finalize_posted
        for mutation in ('book','finance','new_raw_week'):
            case=NativeCompletionTests();case.setUp()
            try:
                case.old_tail()
                with warehouse_functional_job_lock(case.root):
                    args=case.proof_inputs()
                    def changed(service,*a,**kw):
                        if mutation=='book':
                            book,version=accounting.load(case.root)
                            book=deepcopy(book);book['publication_error']='synthetic concurrent book authority'
                            from packages.application.fbs_snapshot_cost import fingerprint
                            next_version=fingerprint(book)
                            with closing(sqlite3.connect(accounting.path(case.root))) as c,c:
                                c.execute('INSERT INTO accounting_revisions VALUES(?,?,?,?)',(next_version,'foreign-book',pack(c,book),version))
                                c.execute('UPDATE accounting_current SET version=?',(next_version,))
                        elif mutation=='finance':
                            with sqlite3.connect(case.runtime.db_path) as c:c.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}'")
                        else:
                            with sqlite3.connect(case.runtime.db_path) as c:
                                # New real native raw cohort, not a fake projection flag.
                                c.execute("UPDATE wb_finance_weekly_raw_rows SET week_start='2026-09-14',week_end='2026-09-20'")
                        return original(service,*a,**kw)
                    with patch.object(FfPoolDocumentService,'_finalize_posted',changed),case.assertRaises(ValueError):case.complete(*args)
                    case.assertEqual(case.native_terminal(case.identity),('posted','mutation_running'))
                    case.assertEqual(operations.read_acceptance(case.runtime.db_path,case.identity)['state'],'processing')
            finally:case.doCleanups()

    def test_missing_native_recovery_is_not_recreated(self):
        self.old_tail()
        with sqlite3.connect(self.runtime.db_path) as c:c.execute('DELETE FROM sheet_vitrina_v1_recovery_operations')
        with warehouse_functional_job_lock(self.root),self.assertRaises(ValueError):operations.capture_pending_completion(self.runtime,finance_block=self.finance)
        with sqlite3.connect(self.runtime.db_path) as c:self.assertEqual(c.execute('SELECT count(*) FROM sheet_vitrina_v1_recovery_operations').fetchone()[0],0)
        self.assertEqual(self.business_image(),self.before)


    def split_finance_raw(self):
        from contextlib import closing
        from packages.application.storage_registry import atomic_write_manifest,build_manifest
        raw=self.root/'native-raw.sqlite3'
        with closing(sqlite3.connect(self.runtime.db_path)) as src,closing(sqlite3.connect(raw)) as dest:src.backup(dest)
        for path,table,logical,revision,generation in (
                (self.runtime.db_path,'finance_operational_schema_meta','operational','operational_v1','native-op'),
                (raw,'finance_raw_schema_meta','finance_raw','finance_raw_v1','native-raw')):
            with closing(sqlite3.connect(path)) as conn,conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute(f'CREATE TABLE IF NOT EXISTS {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT,created_at TEXT)')
                conn.execute(f'INSERT OR REPLACE INTO {table} VALUES(1,?,?,?,?,?,?)',(revision,logical,generation,'fixture','sha256:'+'a'*64,self.stamp()))
                if path==raw:conn.execute('ALTER TABLE wb_finance_weekly_raw_rows RENAME TO finance_raw_current_rows')
        manifest=build_manifest(state='cutover',canonical_source='split',generation_epoch='fixture',raw_generation_id='native-raw',raw_relative_path=raw.name,raw_watermark='1',operational_generation_id='native-op',operational_relative_path=self.runtime.db_path.name,operational_watermark='1',rollback_generation_id='original',source_fingerprint='sha256:'+'a'*64)
        atomic_write_manifest(self.finance.store_registry.manifest_path,manifest)
        return raw

    def test_actual_split_raw_normal_completion_and_attached_raw_race(self):
        original=FfPoolDocumentService._finalize_posted
        for drift in (False,True):
            case=NativeCompletionTests();case.setUp()
            try:
                case.old_tail();raw=case.split_finance_raw()
                with warehouse_functional_job_lock(case.root):
                    args=case.proof_inputs()
                    def changed(service,*a,**kw):
                        if drift:
                            with sqlite3.connect(raw) as c:c.execute("UPDATE finance_raw_current_rows SET row_hash='foreign-row'")
                        return original(service,*a,**kw)
                    with patch.object(FfPoolDocumentService,'_finalize_posted',changed):
                        if drift:
                            with case.assertRaises(ValueError):case.complete(*args)
                            case.assertEqual(case.native_terminal(case.identity),('posted','mutation_running'))
                        else:
                            case.assertEqual(case.complete(*args)['processed_count'],1)
                            case.assertEqual(case.native_terminal(case.identity),('complete','retained'))
                    case.assertEqual(case.business_image(),case.before)
            finally:case.doCleanups()

    def insert_split_week(self,raw):
        with sqlite3.connect(raw) as conn:
            conn.row_factory=sqlite3.Row
            row=dict(conn.execute('SELECT * FROM finance_raw_current_rows LIMIT 1').fetchone())
            row.update(week_start='2026-09-14',week_end='2026-09-20',rrd_id=str(int(row['rrd_id'])+1000),row_hash='synthetic-new-applicable-week')
            conn.execute('INSERT INTO finance_raw_current_rows('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))

    def test_split_scope_insert_final_receipt_before_token_is_rejected(self):
        import inspect
        self.old_tail();raw=self.split_finance_raw();injected=[]
        original=operations._completion_split_tokens
        def token(observer,identity):
            frame=next((f.frame for f in inspect.stack() if f.function=='guard' and f.filename.endswith('operator_ff_overhead.py')),None)
            conn=frame.f_locals.get('conn') if frame else None
            if conn is not None and not injected:
                state=conn.execute(f'SELECT state FROM {operations.TABLE} WHERE request_id=?',(self.identity,)).fetchone()[0]
                if state=='completed':self.insert_split_week(raw);injected.append(True)
            return original(observer,identity)
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(operations,'_completion_split_tokens',side_effect=token),self.assertRaises(ValueError):self.complete(*args)
            self.assertEqual(injected,[True])
            self.assertEqual(self.finance.plan_stale_cost_weeks()['stale_week_count'],1)
        self.assertEqual(operations.read_acceptance(self.runtime.db_path,self.identity)['state'],'processing')
        proof=operations.read_acceptance(self.runtime.db_path,self.identity)['processing_receipt']['native_completion']
        with warehouse_functional_job_lock(self.root):
            operations.drain(self.runtime.db_path,self.root,timestamp_factory=self.stamp)
            self.assertEqual(operations.reconcile(self.runtime,now=self.now)['processed_count'],0)
            acceptance=operations.read_acceptance(self.runtime.db_path,self.identity)
            self.assertEqual(acceptance['processing_receipt']['native_completion'],proof)
            with self.assertRaises(ValueError):operations._finalize_confirmed(self.runtime,acceptance)
            # Restore the intentionally malformed foreign-row fixture. Native Finance
            # refuses that un-ingested week; never promote it with an invented proof.
            with sqlite3.connect(raw) as conn:conn.execute("DELETE FROM finance_raw_current_rows WHERE week_start='2026-09-14'")
            with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',side_effect=AssertionError('duplicate')):
                self.assertEqual(self.complete(*self.proof_inputs())['processed_count'],1)
        self.assertEqual(self.business_image(),self.before)

    def test_split_raw_foreign_commit_outside_nm_scope_keeps_original_token(self):
        self.old_tail();raw=self.split_finance_raw();injected=[]
        original=FfPoolDocumentService._finalize_posted
        def finalize(service,*args,**kwargs):
            with sqlite3.connect(raw) as conn:
                conn.row_factory=sqlite3.Row
                row=dict(conn.execute('SELECT * FROM finance_raw_current_rows LIMIT 1').fetchone())
                operation=json.loads(row['raw_json']);operation['nmId']=999
                row.update(week_start='2026-09-14',week_end='2026-09-20',rrd_id=str(int(row['rrd_id'])+1000),row_hash='synthetic-foreign-nm',raw_json=json.dumps(operation))
                conn.execute('INSERT INTO finance_raw_current_rows('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
            injected.append(True)
            return original(service,*args,**kwargs)
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(FfPoolDocumentService,'_finalize_posted',finalize),self.assertRaises(ValueError):self.complete(*args)
        self.assertEqual(injected,[True])
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
        self.assertEqual(operations.read_acceptance(self.runtime.db_path,self.identity)['state'],'processing')
        self.assertEqual(self.business_image(),self.before)

    def test_split_scope_insert_during_post_own_commit_seal_is_rejected(self):
        self.old_tail();raw=self.split_finance_raw();calls=[]
        original=operations._CompletionReadset.seal
        def seal(reads):
            result=original(reads);calls.append(True)
            if len(calls)==3:self.insert_split_week(raw) # After ack, after scope read, before after-token.
            return result
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(operations._CompletionReadset,'seal',seal),self.assertRaises(ValueError):self.complete(*args)
        self.assertEqual(len(calls),3)
        self.assertEqual(self.native_terminal(self.identity),('posted','mutation_running'))
        self.assertEqual(self.business_image(),self.before)

    def test_two_payment_native_document_parity_and_shared_finance_readset(self):
        from time import monotonic
        from packages.application import fbs_snapshot_cost_sources
        self.payment()
        second=self.preview(request_id='second-native-payment',scope='FBS',amount_rub='125')
        self.surface.confirm_document(second['request_id']);self.drain()
        with warehouse_functional_job_lock(self.root):
            block,plan=self.functional_plan();self.functional=block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=self.root/'backups')
        self.publish(self.prepare());before=self.business_image()
        with operations.readonly(self.runtime.db_path) as conn:
            documents=fbs_snapshot_cost_sources._documents(conn)
            self.assertEqual(len(documents),2)
            for document in documents:
                self.assertEqual(fbs_snapshot_cost_sources._documents(conn,document_id=document['document_id']),[document])
        active=set();heavy=[];holds=[];scans=[];raw_under_writer=[]
        original_connect=sqlite3.connect
        def connect(*args,**kwargs):
            conn=original_connect(*args,**kwargs);started=[None]
            def trace(sql):
                upper=sql.lstrip().upper()
                if upper.startswith('BEGIN IMMEDIATE'):active.add(id(conn));started[0]=monotonic()
                if active and upper.startswith('SELECT') and any(t in sql for t in ('wb_finance_weekly_raw_rows','wb_finance_weekly_aggregates','wb_finance_weekly_sku_aggregates')):raw_under_writer.append(sql)
                if upper.startswith(('COMMIT','ROLLBACK')):
                    active.discard(id(conn))
                    if started[0] is not None:holds.append(monotonic()-started[0]);started[0]=None
            conn.set_trace_callback(trace);return conn
        source=self.finance._finance_source_dependency_fingerprint
        def fingerprint(*args,**kwargs):
            heavy.append(dict(inside_writer=bool(active),keys=sorted(kwargs['target_keys'])))
            return source(*args,**kwargs)
        seal=operations._CompletionReadset.seal
        def stream(reads):
            self.assertFalse(active)
            result=seal(reads);scans.append(result);return result
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(sqlite3,'connect',connect),patch.object(self.finance,'_finance_source_dependency_fingerprint',side_effect=fingerprint),patch.object(operations._CompletionReadset,'seal',stream):
                self.assertEqual(self.complete(*args)['processed_count'],2)
        self.assertEqual(len(heavy),3) # Once native zero-stale plan, scoped cohort, actual applied receipt; independent of payment count.
        self.assertFalse(any(c['inside_writer'] for c in heavy));self.assertEqual(raw_under_writer,[])
        self.assertEqual(self.business_image(),before)
        self.assertEqual(self.native_terminal(second['request_id']),('complete','retained'))
        print('cohort_query_work',json.dumps(dict(payments=2,full_dependency_calls=len(heavy),stream_scans=len(scans),stream_rows=sum(s['rows'] for s in scans),max_writer_seconds=max(holds),raw_image_reads_under_writer=len(raw_under_writer))))

    def test_large_native_pdf_completion_guards_never_select_source_blob(self):
        pdf=_render_pdf(_fixture('wb_bank_0401060.txt'),title='large-native-source',x_offset=0)+b'\n%'+b'padding'*(300000)+b'\n'
        self.old_tail(pdf=pdf)
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT length(source_file_blob) FROM {REQUESTS_TABLE} WHERE request_id=?',(self.identity,)).fetchone()[0],len(pdf))
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            self.assertEqual(self.complete(*args)['processed_count'],1)
        self.assertEqual(self.business_image(),self.before)

    def test_completion_soft_budget_keeps_remainder_durable_then_resumes_without_repost(self):
        import time,inspect
        self.payment();second=self.preview(request_id='second-budget-payment',scope='FBS',amount_rub='125')
        self.surface.confirm_document(second['request_id']);self.drain()
        with warehouse_functional_job_lock(self.root):
            block,plan=self.functional_plan();self.functional=block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=self.root/'backups')
        self.publish(self.prepare());before=self.business_image()
        actual=time.monotonic;started=[]
        def clock():
            caller=inspect.currentframe().f_back.f_code.co_name
            if caller in {'complete_current_cycle','_complete_cohort'}:
                started.append(True);return 0 if len(started)==1 else 31
            return actual()
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(time,'monotonic',clock):result=self.complete(*args)
            self.assertEqual(result,dict(processed_count=1,pending_count=1))
            self.assertEqual(self.native_terminal(args[0]['sources'][1]['request_id']),('posted','mutation_running'))
            with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',side_effect=AssertionError('duplicate')):
                self.assertEqual(self.complete(*self.proof_inputs()),dict(processed_count=1,pending_count=0))
        self.assertEqual(self.business_image(),before)

    def test_writer_requires_pre_dml_fence_and_fresh_transaction_nonce(self):
        from contextlib import closing
        self.old_tail();original=operations._finalize_confirmed;checked=[]
        def finalize(runtime,acceptance,*,prepare_guard,finish):
            def prepare():
                guard=prepare_guard();guard()
                with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                    conn.row_factory=sqlite3.Row
                    changes=conn.total_changes;conn.execute('BEGIN IMMEDIATE')
                    conn.execute(f'UPDATE {operations.TABLE} SET actor=actor WHERE request_id=?',(self.identity,))
                    with self.assertRaises(ValueError):guard(conn,begin=True,initial_changes=changes)
                    conn.rollback()
                    changes=conn.total_changes;conn.execute('BEGIN IMMEDIATE')
                    first=guard(conn,begin=True,initial_changes=changes);conn.rollback()
                    changes=conn.total_changes;conn.execute('BEGIN IMMEDIATE')
                    second=guard(conn,begin=True,initial_changes=changes)
                    self.assertIsNot(first,second)
                    with self.assertRaises(ValueError):guard(conn,transaction=first)
                    conn.rollback();checked.append(True)
                return guard
            return original(runtime,acceptance,prepare_guard=prepare,finish=finish)
        with warehouse_functional_job_lock(self.root):
            args=self.proof_inputs()
            with patch.object(operations,'_finalize_confirmed',finalize):self.assertEqual(self.complete(*args)['processed_count'],1)
        self.assertEqual(checked,[True]);self.assertEqual(self.business_image(),self.before)



if __name__=='__main__':
    unittest.main()
