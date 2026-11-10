#!/usr/bin/env python3
"""Closure-only source drift, reviewed legacy import and immutable money."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import current_fbo_supply_close_smoke as close_fixture
from apps.production_apply_launcher import execute
from packages.application import current_fbo_supply_close as owner
from packages.application import current_fbo_supply_cost_source as source
from packages.application import current_fbo_closure_evidence as evidence
from packages.application import current_fbo_closure_evidence_apply as importing


class ClosureEvidenceTests(unittest.TestCase):
    connection = close_fixture.CurrentFboTests.connection
    snapshot = close_fixture.CurrentFboTests.snapshot

    def setUp(self):
        close_fixture.CurrentFboTests.setUp(self)
        with self.connection() as conn:
            for row in conn.execute('SELECT supply_id,raw_goods_json FROM sheet_vitrina_v1_wb_supplies').fetchall():
                goods = json.loads(row['raw_goods_json'])
                for g in goods:
                    g.update(barcode='barcode-'+row['supply_id'], vendorCode='sku', techSize='0',
                        acceptedQuantity=g['quantity'], supplierBoxAmount=g['quantity'],
                        readyForSaleQuantity=0, unloadingQuantity=g['quantity'])
                conn.execute('UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id=?', (json.dumps(goods), row['supply_id']))
            conn.commit()
        preview = self.adapter.preview(self.request, self.operation)
        self.adapter.apply(self.request, self.operation, preview)
        self.plan = preview['prepared']['plan']

    def change(self, conn, *, goods=None, normalized=None, row_fields=None):
        row = dict(evidence.source_row(conn, '1'))
        if goods:
            value = json.loads(row['raw_goods_json']);goods(value)
            row['raw_goods_json'] = json.dumps(value)
            row['raw_goods_hash'] = owner.digest(value)
        if normalized:
            value = json.loads(row['normalized_row_json']);normalized(value)
            row['normalized_row_json'] = json.dumps(value)
        row.update(row_fields or {})
        keys = ['raw_goods_json', 'raw_goods_hash', 'normalized_row_json', *(row_fields or {})]
        conn.execute('UPDATE sheet_vitrina_v1_wb_supplies SET '+','.join(k+'=?' for k in keys)+" WHERE supply_id='1'", [row[k] for k in keys])

    def monetary(self, conn):
        return {t:owner.rows(conn,t) for t in (owner.RECEIPTS,owner.OPERATIONS_TABLE,owner.LINES_TABLE,
            owner.AGGREGATE,owner.AGGREGATE_LINES,owner.BALANCES_TABLE,owner.RESERVATIONS,owner.RESERVATION_LINES)}

    def legacy(self):
        with self.connection() as conn:
            conn.execute(f'DROP TABLE {evidence.EVIDENCE}')
            conn.execute(f'DROP TABLE {evidence.IMPORTS}')
            conn.commit()
        folder = Path(self.runtime.runtime_dir).parent
        old = folder/'trusted-old.sqlite3'
        with self.connection() as conn, sqlite3.connect(old) as destination:
            conn.backup(destination)
        provenance = dict(origin='trusted fixture native close snapshot', captured_at='2026-08-16T13:00:00Z',
            approved_by='fixture-reviewer', approval_reference='review-42', attests_original_physical_fields=True)
        snapshot = importing.export_snapshot(old, self.operation, ['1','2','3'], folder/'trusted-fbo.json', provenance)
        request = {**self.request, 'closure_operation_id':self.operation, 'trusted_snapshot':snapshot}
        return request, importing.CurrentFboClosureEvidenceAdapter()

    def test_native_baseline_allowed_drift_and_no_second_dispatch(self):
        with self.connection() as conn:
            conn.execute('BEGIN')
            before = self.monetary(conn)
            baseline = evidence.lookup(conn, self.operation, '1')
            self.assertEqual(baseline['old_full_revision'], self.plan['effects'][0]['revision'])
            self.change(conn, goods=lambda g:g[0].update(readyForSaleQuantity=1, unloadingQuantity=1))
            closures = source.capture_warehouse_closures(conn)
            self.assertEqual(closures[0]['source_revision'], self.plan['effects'][0]['revision'])
            self.assertNotEqual(closures[0]['source_revision'], closures[0]['observed_source_revision'])
            self.assertEqual(self.monetary(conn), before)
            self.assertTrue(owner.verify(conn, self.plan)['idempotent'])
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable FBO closure evidence'):
                conn.execute(f'DELETE FROM {evidence.EVIDENCE}')

    def test_forbidden_raw_fields_presence_composition_order_and_type(self):
        mutations = [lambda g,k=k:g[0].update({k:999}) for k in
            ('quantity','acceptedQuantity','supplierBoxAmount','nmID','barcode','vendorCode','techSize')]
        mutations += [lambda g:g[0].update(unknownField=1), lambda g:g[0].pop('readyForSaleQuantity'),
            lambda g:g.append(deepcopy(g[0])), lambda g:g.pop(), lambda g:g[0].update(quantity=True)]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.connection() as conn:
                conn.execute('BEGIN');self.change(conn,goods=mutation)
                with self.assertRaises(ValueError):source.capture_warehouse_closures(conn)
                conn.rollback()
        with self.connection() as conn:
            conn.execute('BEGIN')
            self.change(conn, goods=lambda g:g.append(dict(g[0],nmID=102,barcode='different')))
            with self.assertRaises(ValueError):source.capture_warehouse_closures(conn)
            conn.rollback()

    def test_status_date_source_identity_type_and_exact_revision_guards(self):
        for field, value in [('status_id',4),('updated_date','2026-09-01'),('supply_date','2026-09-01'),
                ('virtual_type_id',1),('type_label','changed physical type'),('cache_key','different')]:
            with self.subTest(field=field), self.connection() as conn:
                conn.execute('BEGIN')
                self.change(conn,normalized=lambda n:n.update({field:value}))
                with self.assertRaises(ValueError):source.capture_warehouse_closures(conn)
                conn.rollback()
        # Neither exact global revision nor missing legacy baseline bypasses
        # current ordinary physical validation or active box corrections.
        with self.connection() as conn:
            conn.execute('BEGIN')
            self.change(conn,normalized=lambda n:n.update(virtual_type_id=owner.ledger.WB_SKIP_VIRTUAL_TYPE_ID))
            record=owner._wb_supply_record_from_row(evidence.source_row(conn,'1'))
            self.assertEqual(owner.ledger._wb_supply_revision(record,record['normalized']),self.plan['effects'][0]['revision'])
            with self.assertRaisesRegex(ValueError,'ordinary_physical'):source.capture_warehouse_closures(conn)
            conn.rollback()
        from packages.application import wb_supply_box_correction as boxes
        with self.connection() as conn, patch.object(boxes,'load_active_box_correction',return_value={'active':True}):
            with self.assertRaisesRegex(ValueError,'box_correction_requires_review'):source.capture_warehouse_closures(conn)

    def test_multiple_goods_order_is_part_of_closure_proof(self):
        with self.connection() as conn:
            record=owner._wb_supply_record_from_row(evidence.source_row(conn,'1'))
            record['raw_goods'].append(dict(record['raw_goods'][0],nmID=102,barcode='second-row'))
            plan=deepcopy(self.plan);effect=plan['effects'][0]
            revision=owner.ledger._wb_supply_revision(record,record['normalized'])
            effect['revision']=revision
            for line in effect['lines']:line['source_revision']=revision
            plan['plan_digest']=owner.digest({k:v for k,v in plan.items() if k!='plan_digest'})
            baseline=evidence.payload(plan,effect,record,dict(kind='test_source'))
            changed=deepcopy(record);changed['raw_goods'].reverse()
            with self.assertRaisesRegex(ValueError,'source_revision_changed'):
                evidence.verify_closure(conn,record=changed,plan=plan,effect=effect,baseline=baseline)

    def test_existing_domain_rollback_removes_new_baseline_with_its_dispatch(self):
        from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
        result=self.adapter.readback(self.request,self.operation)
        book=Path(self.runtime.runtime_dir)/'fbs-snapshot-accounting.sqlite3'
        before_book=book.read_bytes()
        registry=WarehouseRecoveryRegistry(runtime_dir=self.runtime.runtime_dir,db_path=self.runtime.db_path)
        registry.rollback_t2(result['recovery']['operation_id'],reason='isolated closure rollback test')
        with self.connection() as conn:
            self.assertFalse(evidence.exists(conn,evidence.EVIDENCE))
            self.assertFalse(evidence.exists(conn,evidence.IMPORTS))
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {owner.RECEIPTS}').fetchone()[0],0)
            balance=conn.execute(f"SELECT quantity FROM {owner.BALANCES_TABLE} WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101").fetchone()[0]
            self.assertEqual(balance,10)
        self.assertEqual(book.read_bytes(),before_book)

    def test_legacy_import_exact_proof_allowed_drift_matching_noop_conflict_and_readback(self):
        request, adapter = self.legacy()
        with self.connection() as conn:
            before = self.monetary(conn)
            self.assertEqual(len(source.capture_warehouse_closures(conn)),3)
            self.change(conn,goods=lambda g:g[0].update(readyForSaleQuantity=1,unloadingQuantity=1));conn.commit()
            with self.assertRaisesRegex(ValueError,'unproven_baseline'):source.capture_warehouse_closures(conn)
        preview=adapter.preview(request,'evidence-import-01')
        result=execute(action='apply',adapter_name='fixture',operation_id='evidence-import-01',request=request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'fixture':adapter})
        self.assertEqual(result['state'],'applied');self.assertEqual(result['readback']['created'],3)
        self.assertEqual(adapter.apply(request,'evidence-import-01',preview)['disposition'],'already_applied')
        with self.connection() as conn:
            self.assertEqual(self.monetary(conn),before)
            self.assertEqual(len(source.capture_warehouse_closures(conn)),3)
        preview=adapter.preview(request,'evidence-import-02')
        adapter.apply(request,'evidence-import-02',preview)
        self.assertEqual(adapter.readback(request,'evidence-import-02')['created'],0)
        changed=deepcopy(request);changed['trusted_snapshot']['provenance']['approved_by']='another'
        with self.assertRaisesRegex(ValueError,'existing_request_conflict'):adapter.readback(changed,'evidence-import-01')
        # Same original closure/supply cannot acquire a second provenance or
        # baseline through a different import operation.
        path=Path(request['trusted_snapshot']['path']);artifact=json.loads(path.read_text())
        artifact['provenance']=changed['trusted_snapshot']['provenance'];path.write_text(json.dumps(artifact))
        with self.assertRaisesRegex(ValueError,'closure_evidence_conflict'):adapter.preview(changed,'evidence-import-03')
        # Readback uses committed evidence and native receipts, not the continued
        # availability of the transferred historical artifact.
        Path(request['trusted_snapshot']['path']).unlink()
        self.assertEqual(adapter.readback(request,'evidence-import-01')['state'],'applied')

    def test_legacy_artifact_hash_revision_origin_and_cas_rejections(self):
        request,adapter=self.legacy()
        direct=deepcopy(request);direct['trusted_snapshot']['path']=str(Path(request['trusted_snapshot']['path']).with_name('trusted-old.sqlite3'))
        self.assertEqual(adapter.preview(direct,'evidence-direct-01')['summary']['evidence'],3)
        changed=deepcopy(request);changed['trusted_snapshot']['source_digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError,'snapshot_digest_changed'):adapter.preview(changed,'evidence-import-01')
        changed=deepcopy(request);changed['trusted_snapshot']['provenance']['attests_original_physical_fields']=False
        with self.assertRaisesRegex(ValueError,'attestation_required'):adapter.preview(changed,'evidence-import-01')
        path=Path(request['trusted_snapshot']['path']);original=json.loads(path.read_text())
        value=deepcopy(original);row=value['material']['supplies'][0];goods=json.loads(row['raw_goods_json'])
        goods[0]['quantity']+=1;row['raw_goods_json']=json.dumps(goods);path.write_text(json.dumps(value))
        changed=deepcopy(request);changed['trusted_snapshot']['source_digest']=owner.digest(value['material'])
        with self.assertRaisesRegex(ValueError,'old_full_revision_mismatch'):adapter.preview(changed,'evidence-import-01')
        path.write_text(json.dumps(original))
        preview=adapter.preview(request,'evidence-import-01')
        with self.connection() as conn:
            self.change(conn,goods=lambda g:g[0].update(readyForSaleQuantity=1,unloadingQuantity=1));conn.commit()
        with self.assertRaisesRegex(ValueError,'preview_compare_and_swap'):adapter.apply(request,'evidence-import-01',preview)
        self.assertEqual(adapter.readback(request,'evidence-import-01')['state'],'not_submitted')


if __name__ == '__main__':unittest.main()
