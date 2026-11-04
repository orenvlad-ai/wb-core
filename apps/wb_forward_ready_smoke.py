#!/usr/bin/env python3
"""Native forward Ready ownership with synthetic sources and a disposable store."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.ready_publication_smoke import seed as registry_seed, make_plan, clock, make_book
from apps.fbs_snapshot_cost_smoke import capture, document
from apps.shared_sku_cost_smoke import wb
from apps.fbs_inventory_presentation_smoke import retained
from apps.warehouse_wb_initial_valuation_smoke import seed, basis, payload, DAY, NOW
from packages.application import registry_upload_db_backed_runtime as dbmod
from packages.application import wb_forward_ready as admission
from packages.application import wb_forward_valuation as forward
from packages.application import web_vitrina_management_history as economics
from packages.application import sheet_vitrina_v1_inventory_history as history
from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application.warehouse_functional import WarehouseFunctionalBlock
from packages.application.ff_pool_documents import ensure_ff_pool_document_schema
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot

CUTOFF = '2026-10-09'
REQUEST = dict(request_id='fixture-forward-1', day=CUTOFF, shipment_id='supplier', receipt_id='receipt', nm_ids=[200])
ALLOCATION = dict(blockers=[], financial_readiness={'ready':True}, lines=[dict(nm_id=200, quantity='2', unit_cost_rub='120')])
SOURCE = ({'source_fingerprint':'sha256:source'}, ALLOCATION)


def at(day):
    return datetime.fromisoformat(day).replace(hour=14, tzinfo=timezone.utc)


def template(day=CUTOFF, previous=DAY):
    plan = make_plan(as_of_date=previous, day=day)
    rows = deepcopy(plan.sheets[0].rows)
    rows.append(['profit', 'SKU:1|proxy_profit_3_rub', 321.12, ''])
    return replace(plan, as_of_date=previous,
        temporal_slots=[SheetVitrinaV1TemporalSlot('yesterday_closed','closed',previous),
                        SheetVitrinaV1TemporalSlot('today_current','current',day)],
        sheets=[replace(plan.sheets[0], rows=rows, row_count=len(rows)), plan.sheets[1]],
        metadata={'server_cell_presentation': {'SKU:1|proxy_profit_3_rub': {previous: {'quality_state':'fixture_frozen', 'source':'saved'}}}})


def saved(runtime, plan):
    current = runtime.load_current_state()
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        expected = ready.capture_expected(conn, bundle_version=current.bundle_version, as_of_date=plan.as_of_date)
        conn.execute("BEGIN IMMEDIATE")
        ready.replace_ready(conn, expected=expected, plan_json=dbmod._serialize_sheet_vitrina_plan(plan),
            activated_at=current.activated_at, snapshot_id=plan.snapshot_id, plan_version=plan.plan_version, refreshed_at=NOW)


class ForwardReadyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='forward-ready-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = registry_seed(self.root)
        self.block = WarehouseFunctionalBlock(runtime=self.runtime, timestamp_factory=lambda: DAY+'T09:00:00Z')
        seed(self.block)
        self.block.timestamp_factory=lambda: NOW
        with patch('packages.application.wb_initial_fbs_cost_sources.capture', return_value=basis(self.block._active_version_id())):
            p = self.block._build_plan(kind='hourly_wb_sync', wb_payload=payload())
            self.block.apply_plan(p, confirm_fingerprint=p['plan_fingerprint'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            ensure_ff_pool_document_schema(conn)
            values=dict(document_id='receipt',request_id='request',document_role='root',document_kind='china_acceptance',root_document_id='receipt',operation_id='operation',source_system='supplier_registry',source_type='china_acceptance_form',source_id='supplier',source_revision='1',idempotency_epoch=1,actor='fixture',business_date=DAY,posted_manifest_sha256='sha256:fixture',posted_manifest_json='{}',posted_at=NOW)
            conn.execute('INSERT INTO sheet_vitrina_v1_ff_pool_documents('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
            conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_document_lines VALUES('receipt',1,'receipt','accepted_pool_allocation',NULL,'FBS',200,2,'200','0','{}')")
        self.source = patch('packages.application.operator_supplier_cost_proof.numerical_supplier_state', return_value=SOURCE)
        self.source.start(); self.addCleanup(self.source.stop)
        self.block.timestamp_factory=lambda: CUTOFF+'T10:00:00Z'
        p=forward.prepare_plan(self.block,request=REQUEST,wb_payload=payload('1','0',CUTOFF+'T10:00:00Z'))
        self.block.apply_plan(p,confirm_fingerprint=p['plan_fingerprint'])
        self.archived = template(day=DAY, previous='2026-10-07')
        saved(self.runtime, self.archived)
        self.old = replace(template(), as_of_date=DAY)
        saved(self.runtime, self.old)
        self.plan=template()

    def prepare(self, plan=None, day=CUTOFF):
        plan=plan or self.plan
        expected=self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=self.runtime.load_current_state().bundle_version,as_of_date=plan.as_of_date)
        with ready.readonly(self.runtime.db_path) as conn:
            proof=admission.prepare_forward_ready_admission(conn,request=REQUEST,plan=plan,expected=expected,publication_date=day)
        return expected,proof

    def save(self, plan, expected, proof, day=CUTOFF):
        with clock(at(day)):
            return self.runtime.save_sheet_vitrina_ready_snapshot(current_state=self.runtime.load_current_state(),
                refreshed_at=day+'T14:00:00Z',plan=plan,expected=expected,forward_admission=proof)

    def test_native_writer_preserves_history_and_never_recalculates_pre_cutoff(self):
        expected,proof=self.prepare()
        old_json=dbmod._serialize_sheet_vitrina_plan(self.archived)
        original_prepare=history.prepare_inventory_history_from_ready_plan
        captured=[]
        def observe(*args,**kwargs):
            result=original_prepare(*args,**kwargs);captured.append(result);return result
        with patch.object(economics,'recalculate_yesterday_envelope',wraps=economics.recalculate_yesterday_envelope) as yesterday, \
             patch.object(economics,'recalculate_dated_proxy',wraps=economics.recalculate_dated_proxy) as dated, \
             patch.object(history,'prepare_inventory_history_from_ready_plan',side_effect=observe):
            self.save(self.plan,expected,proof)
        yesterday.assert_not_called();dated.assert_not_called()
        self.assertTrue(captured);self.assertEqual(captured[0]['closed_date'],'')
        with ready.readonly(self.runtime.db_path) as conn:
            old=ready.capture_expected(conn,bundle_version=expected.bundle_version,as_of_date=self.archived.as_of_date)
            new=ready.capture_expected(conn,bundle_version=expected.bundle_version,as_of_date=DAY)
            self.assertEqual(old.plan_json,old_json)
            self.assertFalse(conn.execute('SELECT 1 FROM sheet_vitrina_v1_inventory_history_finalizations WHERE business_date<?',(CUTOFF,)).fetchall())
        plan=dbmod._deserialize_sheet_vitrina_plan(new.plan_json)
        self.assertEqual(admission.forward.digest(proof['frozen']),plan.metadata['wb_forward_ready']['frozen_cells_digest'])
        self.assertEqual(admission._rows(admission._data(plan))['SKU:1|proxy_profit_3_rub'][2],321.12)
        self.assertEqual(plan.metadata['server_cell_presentation']['SKU:1|proxy_profit_3_rub'][DAY],{'quality_state':'fixture_frozen','source':'saved'})

    def test_bad_and_stale_proofs_rejected(self):
        expected,proof=self.prepare()
        bad=deepcopy(proof);bad['cutoff']=DAY
        with self.assertRaisesRegex(ValueError,'proof_changed'):
            self.save(self.plan,expected,bad)
        changed=deepcopy(ALLOCATION);changed['lines'][0]['unit_cost_rub']='121'
        with patch('packages.application.operator_supplier_cost_proof.numerical_supplier_state',return_value=({'source_fingerprint':'different'},changed)):
            with self.assertRaisesRegex(ValueError,'identity_conflict'):
                self.save(self.plan,expected,proof)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=replace(plan_json,'321.12','321.13') WHERE as_of_date=?",(DAY,))
        with self.assertRaisesRegex(ValueError,'proof_changed|ready_target_changed'):
            self.save(self.plan,expected,proof)

    def test_invalid_temporal_structure_rejected_before_ready_write(self):
        expected, proof = self.prepare()
        missing_slot = replace(self.plan, temporal_slots=self.plan.temporal_slots[-1:])
        mismatched_header = replace(self.plan, sheets=[replace(self.plan.sheets[0],
            header=['label', 'key', DAY, '2026-10-10']), self.plan.sheets[1]])
        duplicate_date = replace(self.plan, date_columns=[DAY, DAY],
            temporal_slots=[self.plan.temporal_slots[0], self.plan.temporal_slots[0]],
            sheets=[replace(self.plan.sheets[0], header=['label', 'key', DAY, DAY]), self.plan.sheets[1]])
        for bad, reason in ((missing_slot, 'temporal_slots'),
                (mismatched_header, 'date_columns must match'), (duplicate_date, 'duplicate_date_columns')):
            with self.subTest(reason=reason), patch.object(ready, 'record_intent', wraps=ready.record_intent) as intent:
                with self.assertRaisesRegex(ValueError, reason):
                    self.save(bad, expected, proof)
                intent.assert_not_called()
            with ready.readonly(self.runtime.db_path) as conn:
                ready.check_expected(conn, expected)

    def test_duplicate_request_and_persisted_event_rejected(self):
        expected, _ = self.prepare()
        duplicate = {**REQUEST, 'nm_ids': [200, 200]}
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ValueError, 'duplicate_request_scope'):
                admission.prepare_forward_ready_admission(conn, request=duplicate,
                    plan=self.plan, expected=expected, publication_date=CUTOFF)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("""INSERT INTO sheet_vitrina_v1_warehouse_functional_events
                SELECT event_id||'-duplicate',version_id,event_type,source_id,
                    source_fingerprint||'-duplicate',business_date,nm_id,quantity,
                    capital_rub,provenance_json,created_at
                FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type=?""", (forward.EVENT,))
        with self.assertRaisesRegex(ValueError, 'event_scope'):
            self.prepare()

    def test_same_day_persisted_policy_does_not_recalculate_old_profit(self):
        expected, proof = self.prepare()
        self.save(self.plan, expected, proof)
        retained = self.runtime.load_sheet_vitrina_ready_snapshot(DAY)
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(
            bundle_version=expected.bundle_version, as_of_date=DAY)
        with patch.object(economics,'recalculate_yesterday_envelope',wraps=economics.recalculate_yesterday_envelope) as yesterday:
            self.save(retained, expected, None)
        yesterday.assert_not_called()
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type=?',(forward.EVENT,)).fetchone()[0],1)

    def test_semantic_identity_reordering_and_missing_dates(self):
        expected,proof=self.prepare()
        reordered=replace(self.plan,sheets=[replace(self.plan.sheets[0],rows=list(reversed(self.plan.sheets[0].rows))),self.plan.sheets[1]])
        restored=admission.restore_frozen(admission.forward_view(reordered,CUTOFF),original=reordered,admission=proof)
        self.assertEqual(admission._rows(admission._data(restored))['SKU:1|proxy_profit_3_rub'][2],321.12)
        binding={'book_version':'saved-book','presentation_version':'saved-presentation','ready_target':{'bundle_version':expected.bundle_version,'as_of_date':DAY}}
        with_binding=deepcopy(proof)
        with_binding['frozen'][DAY]['metadata']['fbs_accounting_bindings']=binding
        with_binding['frozen'][DAY]['metadata']['fbs_accounting_targets']=binding['ready_target']
        retained=admission.restore_frozen(admission.forward_view(reordered,CUTOFF),original=reordered,admission=with_binding)
        self.assertEqual(retained.metadata['fbs_accounting_bindings'][DAY],binding)
        self.assertEqual(retained.metadata['fbs_accounting_targets'][DAY],binding['ready_target'])
        current_only=replace(admission.forward_view(self.plan,CUTOFF),as_of_date=CUTOFF)
        _,current_proof=self.prepare(current_only)
        self.assertEqual(current_proof['frozen'],{})
        self.assertEqual(admission.restore_frozen(current_only,original=current_only,admission=current_proof).date_columns,[CUTOFF])

    def test_next_day_native_yesterday_closure_allowed(self):
        expected,proof=self.prepare();self.save(self.plan,expected,proof)
        next_day='2026-10-10'
        self.block.timestamp_factory=lambda:next_day+'T10:00:00Z'
        p=self.block._build_plan(kind='hourly_wb_sync',wb_payload=payload('1','0',next_day+'T10:00:00Z'))
        self.block.apply_plan(p,confirm_fingerprint=p['plan_fingerprint'])
        plan=template(next_day,CUTOFF)
        expected,proof=self.prepare(plan,day=next_day)
        with patch.object(economics,'recalculate_yesterday_envelope',wraps=economics.recalculate_yesterday_envelope) as yesterday:
            self.save(plan,expected,proof,day=next_day)
        self.assertEqual(yesterday.call_count,1)
        self.assertEqual(yesterday.call_args.kwargs['business_date'],next_day)

    def test_real_refresh_rebuilds_only_post_cutoff_dates(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES('synthetic-item',1,0,1,'Synthetic','test','synthetic','[]',?,?)",(NOW,NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES('known-item',1,0,104,'Known','test','known','[]',?,?)",(NOW,NOW))
        initial, version = make_book(self.root, now=at(CUTOFF), opening=True, typed=True)
        accounting.save(self.root, initial, expected=version, operation_id='opening')
        expected, proof = self.prepare()
        self.save(self.plan, expected, proof)
        next_day = '2026-10-10'
        stamp = next_day+'T14:00:00Z'
        self.block.timestamp_factory = lambda: stamp
        p = self.block._build_plan(kind='hourly_wb_sync', wb_payload=payload('1','0',stamp))
        self.block.apply_plan(p, confirm_fingerprint=p['plan_fingerprint'])
        captured = capture(next_day, quantity='1000', docs=[document(day=next_day)], timestamp=stamp)
        component = wb(next_day)
        captured['quantity_snapshot'].update(source='official_fbs_stock_snapshot_v1', facility_evidence={
            'ff-1': {'facility_name':'Fixture','stock_run_id':'run-'+next_day,
                'stock_digest':'digest-'+next_day,'mapping_id':'mapping-1','captured_at':stamp}})
        component.update(source={'snapshot_date':next_day,'fetched_at':stamp},authority_complete=True,requested_nm_ids=[1])
        component['rows'][0]['components']={'physical':'500'}
        component['source_digest']=accounting.fingerprint({k:v for k,v in component.items() if k!='source_digest'})
        captured['source_digest']=accounting.fingerprint({k:v for k,v in captured.items() if k!='source_digest'})
        original_prepare = history.prepare_inventory_history_from_ready_plan
        histories = []
        built = []
        native_build = SheetVitrinaV1LivePlanBlock.build_plan
        def observe_build(*args, **kwargs):
            result = native_build(*args, **kwargs); built.append(result); return result
        def observe(*args, **kwargs):
            result = original_prepare(*args, **kwargs); histories.append(result); return result
        # Input fixtures supply source facts; prepare/refresh/materializers/locks,
        # book closure, saved policy discovery, and Ready commits all stay native.
        with clock(at(next_day)), patch.object(accounting,'capture_current',return_value=captured), \
             patch.object(accounting,'capture_wb_component',return_value=component), \
             patch.object(accounting,'capture_retained_stages',return_value=retained(component)), \
             patch('packages.application.operator_supplier_cost_proof.numerical_supplier_state',side_effect=AssertionError('sealed supplier source rebuilt')), \
             patch.object(SheetVitrinaV1LivePlanBlock,'build_plan',autospec=True,side_effect=observe_build), \
             patch.object(economics,'recalculate_yesterday_envelope',wraps=economics.recalculate_yesterday_envelope) as yesterday, \
             patch.object(history,'prepare_inventory_history_from_ready_plan',side_effect=observe):
            result = accounting.refresh(self.root, ready_runtime=self.runtime)
        self.assertEqual(result['status'],'published')
        self.assertEqual(yesterday.call_count,1)
        self.assertTrue(histories)
        self.assertEqual(histories[0]['closed_date'],CUTOFF)
        self.assertEqual(len(built),1)
        self.assertEqual(built[0].date_columns,[CUTOFF,next_day])
        built_data=admission._data(built[0])
        self.assertEqual(admission._rows(built_data)['SKU:104|own_capital_WB_capital_rub'][built_data.header.index(next_day)],10.0)
        book, _ = accounting.load(self.root)
        self.assertEqual(book['state']['periods'][CUTOFF]['status'],'closed')
        with ready.readonly(self.runtime.db_path) as conn:
            target = ready.capture_expected(conn,bundle_version=expected.bundle_version,as_of_date=CUTOFF)
            old = ready.capture_expected(conn,bundle_version=expected.bundle_version,as_of_date=self.archived.as_of_date)
            self.assertEqual(old.plan_json,dbmod._serialize_sheet_vitrina_plan(self.archived))
            self.assertFalse(conn.execute('SELECT 1 FROM sheet_vitrina_v1_inventory_history_finalizations WHERE business_date<?',(CUTOFF,)).fetchall())
        advanced = dbmod._deserialize_sheet_vitrina_plan(target.plan_json)
        self.assertIn(next_day,advanced.date_columns)
        self.assertEqual(advanced.metadata['wb_forward_ready']['cutoff'],CUTOFF)
        self.assertEqual(advanced.date_columns,[CUTOFF,next_day])
        self.assertNotIn(DAY,advanced.date_columns)

    def test_native_book_materializer_receives_forward_dates_only(self):
        expected,proof=self.prepare()
        book,_=make_book(self.root,now=at(CUTOFF),opening=True)
        view=admission.forward_view(self.plan,CUTOFF)
        materialized=accounting.materialize(view,runtime_dir=self.root,now=at(CUTOFF),book=book,
            book_version=accounting.fingerprint(book),ready_target={'bundle_version':expected.bundle_version,'as_of_date':CUTOFF})
        self.assertNotIn(DAY,materialized.metadata['fbs_accounting_bindings'])
        restored=admission.restore_frozen(materialized,original=self.plan,admission=proof)
        self.assertEqual(admission._rows(admission._data(restored))['SKU:1|our_wb_unit_cost_rub'][2],91)
        self.assertNotEqual(admission._rows(admission._data(restored))['SKU:1|our_wb_unit_cost_rub'][3],92)

if __name__=='__main__':unittest.main()
