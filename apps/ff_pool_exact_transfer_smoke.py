#!/usr/bin/env python3
"""Local exact-capital full/partial pool transfers; no synthetic monetary rounding."""
from copy import deepcopy
from decimal import Decimal,localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application import ff_pool_documents as docs
from packages.application.ff_pool_foundation import BALANCES_TABLE,canonical_decimal_ratio_text
from apps.ff_pool_documents_smoke import Clock,_seed,identity
from packages.application.fbs_snapshot_cost_sources import _verified_manifest, _verify_typed_lines

class ExactTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=Clock()
        self.service=docs.FfPoolDocumentService(db_path=self.root/'test.sqlite3',runtime_dir=self.root,timestamp_factory=self.clock)
        _seed(self.service,self.clock)
        self.remaining={}
        # Published original source amounts plus independently evidenced handoff
        # quantities, reproduced using the native stored-ratio policy.
        for nm,q,cost,sent in [(1,1600,'214330',8),(2,1800,'241121.26',12),(3,1200,'157678.51',8),(4,800,'105119.01',4)]:
            with localcontext() as context:
                context.prec=160
                price=Decimal(canonical_decimal_ratio_text(Decimal(cost),q))
                remaining=Decimal(cost)-price*sent
                self.remaining[nm]=(q-sent,remaining)
        with sqlite3.connect(self.service.db_path) as conn:
            for nm,(q,c) in self.remaining.items():
                conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',('fac_msk','FBS',nm,1,q,format(c,'f'),canonical_decimal_ratio_text(c,q),'local-dated-handoffs',self.clock()))
                conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',('fac_msk','FBO',nm,1,0,'0',None,'local-original-transfer',self.clock()))
    def manifest(self):
        return dict(facility_id='fac_msk',source_pool='FBS',destination_pool='FBO',items=[dict(nm_id=nm,quantity=q) for nm,(q,_) in self.remaining.items()],expenses=[])
    def plan(self,manifest=None):
        with sqlite3.connect(self.service.db_path) as conn:
            conn.row_factory=sqlite3.Row
            return docs._plan_pool_reallocation(conn,request={'request_identity':'sha256:'+'1'*64},manifest=manifest or self.manifest(),epoch=1)
    def test_full_return_posts_exact_money_and_zeroes_source(self):
        preview=self.service.accept_preview(identity=identity('exact-return'),document_kind='pool_reallocation',manifest=self.manifest())
        self.assertEqual(preview['state'],'ready')
        result=self.service.post(preview['request_id'])
        self.assertIn(result['state'],{'posted','complete'})
        with sqlite3.connect(self.service.db_path) as conn:
            conn.row_factory=sqlite3.Row
            for row in conn.execute(f'SELECT * FROM {BALANCES_TABLE}'):
                q,c=self.remaining[row['nm_id']]
                self.assertEqual((row['quantity'],Decimal(row['capital_rub'])),(0,Decimal(0)) if row['pool']=='FBS' else (q,c))
            stored=list(conn.execute(f'SELECT capital_rub FROM {docs.DOCUMENT_LINES_TABLE} ORDER BY nm_id'))
            self.assertEqual([Decimal(r[0]) for r in stored],[c for _,c in self.remaining.values()])
            from packages.application.fbs_snapshot_cost_sources import _verified_manifest,_verify_typed_lines
            document=dict(conn.execute(f'SELECT * FROM {docs.DOCUMENTS_TABLE}').fetchone())
            lines=[dict(r) for r in conn.execute(f'SELECT * FROM {docs.DOCUMENT_LINES_TABLE} ORDER BY line_no')]
            movements=[dict(r) for r in conn.execute(f'SELECT * FROM {docs.LINES_TABLE} ORDER BY line_no')]
            _verify_typed_lines(document,_verified_manifest(document),lines,movements)
            changed=deepcopy(lines);changed[1]['capital_rub']='1'
            with self.assertRaises(ValueError):_verify_typed_lines(document,_verified_manifest(document),changed,movements)

        repeated=self.service.post(preview['request_id'])
        self.assertEqual(repeated['state'],result['state'])
        with sqlite3.connect(self.service.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {docs.DOCUMENTS_TABLE}').fetchone()[0],1)
    def test_full_reverse_transfer_preserves_exact_capital_without_rounding(self):
        for index,(source,destination) in enumerate((('FBS','FBO'),('FBO','FBS'))):
            manifest=self.manifest();manifest.update(source_pool=source,destination_pool=destination)
            request=self.service.accept_preview(identity=identity('full-reverse-'+str(index)),document_kind='pool_reallocation',manifest=manifest)
            self.service.post(request['request_id'])
        with sqlite3.connect(self.service.db_path) as conn:
            conn.row_factory=sqlite3.Row
            for row in conn.execute(f'SELECT * FROM {BALANCES_TABLE}'):
                q,c=self.remaining[row['nm_id']]
                self.assertEqual((row['quantity'],Decimal(row['capital_rub'])),(q,c) if row['pool']=='FBS' else (0,Decimal(0)))
        plan=self.plan();plan['posted_manifest']={}
        preview=docs._posting_plan_preview(plan=plan,epoch=1)
        self.assertEqual(Decimal(preview['capital_delta_rub']),0)
        with localcontext() as context:
            context.prec=160
            expected=sum((c for _,c in self.remaining.values()),Decimal(0))
            self.assertEqual(Decimal(preview['capital_delta_rub_by_pool']['FBS']),-expected)
            self.assertEqual(Decimal(preview['capital_delta_rub_by_pool']['FBO']),expected)

    def test_expensed_fractional_capital_is_not_rounded(self):
        manifest=self.manifest()
        manifest['expenses']=[dict(amount_rub='1.00',basis='test')]
        with self.assertRaises(docs.FfPoolDocumentError) as caught:self.plan(manifest)
        self.assertEqual(caught.exception.code,'exact_full_pool_transfer_required')
    def test_tampered_exact_pair_cannot_apply(self):
        document=self.plan()['documents'][0]
        movement=next(m for m in document['movements'] if 'capital_delta_exact_rub' in m)
        movement['capital_delta_exact_rub']='-1'
        with self.assertRaises(docs.FfPoolDocumentError):docs._validate_exact_pool_transfer(document)
    def test_source_tail_or_quantity_change_cannot_be_drained(self):
        document=self.plan()['documents'][0]
        source=next(m for m in document['movements'] if 'capital_delta_exact_rub' in m and m['quantity_delta']<0)
        q,c=self.remaining[source['nm_id']]
        with self.assertRaises(docs.FfPoolDocumentError):
            docs._validate_balance_effect(before_quantity=q,before_capital=c+Decimal('0.01'),movement=source)

class OrdinaryCapitalCompatibilityTests(unittest.TestCase):
    def test_ordinary_cents_preserve_legacy_text_without_context_rounding(self):
        for cents, expected in ((0,'0'),(100,'1'),(10,'0.1'),(1,'0.01'),(-100,'-1'),
                (12345678901234567890123456789012345678901,
                 '123456789012345678901234567890123456789.01')):
            with self.subTest(cents=cents),localcontext() as context:
                context.prec=6
                self.assertEqual(str(docs._movement_capital_rub({'capital_delta_cents':cents})),expected)
        quantity,capital=docs._validate_balance_effect(before_quantity=0,before_capital=Decimal(0),
            movement={'quantity_delta':1,'capital_delta_cents':100})
        self.assertEqual((quantity,str(capital)),(1,'1'))

class PartialTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.clock = Clock()
        self.service = docs.FfPoolDocumentService(db_path=self.root/'test.sqlite3', runtime_dir=self.root,
                                                timestamp_factory=self.clock)
        _seed(self.service, self.clock)
        self.quantity = 97
        self.capital = Decimal('12889.95368000000000000000000000000000000032')
        with sqlite3.connect(self.service.db_path) as conn:
            for pool, q, c in [('FBS', self.quantity, self.capital), ('FBO', 7, Decimal('800.50'))]:
                conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',
                             ('fac_msk', pool, 1, 1, q, str(c), canonical_decimal_ratio_text(c,q), 'fixture', self.clock()))

    def manifest(self, q=11):
        return dict(facility_id='fac_msk', source_pool='FBS', destination_pool='FBO',
                    items=[dict(nm_id=1, quantity=q)], expenses=[])

    def plan(self, manifest=None):
        with sqlite3.connect(self.service.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return docs._plan_pool_reallocation(conn, request={'request_identity':'sha256:'+'1'*64},
                                               manifest=manifest or self.manifest(), epoch=1)

    def preview(self, name='partial', q=11):
        return self.service.accept_preview(identity=identity(name), document_kind='pool_reallocation', manifest=self.manifest(q))

    def balances(self):
        with sqlite3.connect(self.service.db_path) as conn:
            return {p:(q, Decimal(c)) for p,q,c in conn.execute(f'SELECT pool,quantity,capital_rub FROM {BALANCES_TABLE}')}

    def reserve(self, quantity):
        from packages.application.ff_pool_cutover import ensure_ff_pool_cutover_schema, MANIFESTS_TABLE
        from packages.application.ff_pool_fbs_lifecycle import ensure_ff_pool_fbs_lifecycle_schema, CURRENT_TABLE
        with sqlite3.connect(self.service.db_path) as conn:
            ensure_ff_pool_cutover_schema(conn); ensure_ff_pool_fbs_lifecycle_schema(conn)
            columns = conn.execute(f'PRAGMA table_info({MANIFESTS_TABLE})').fetchall()
            values = {r[1]:(0 if r[2]=='INTEGER' else 'fixture') for r in columns}
            values.update(cutover_id='reserve-fixture',manifest_digest='sha256:'+'a'*64,deployed_sha='a'*40,
                          cutover_at=self.clock(),business_date='2026-01-01',feature_epoch=1,created_at=self.clock(),manifest_json='{}')
            conn.execute(f'INSERT INTO {MANIFESTS_TABLE}({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))
            columns = conn.execute(f'PRAGMA table_info({CURRENT_TABLE})').fetchall()
            values = {r[1]:(1 if r[2]=='INTEGER' else 'fixture') for r in columns}
            values.update(order_id=1,cutover_id='reserve-fixture',facility_id='fac_msk',pool='FBS',nm_id=1,
                          quantity=quantity,state='reserved',frozen_wac_rub='10',updated_at=self.clock())
            conn.execute(f'INSERT INTO {CURRENT_TABLE}({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))
        return CURRENT_TABLE

    def test_partial_post_conserves_every_digit_and_keeps_reservations_and_saved_cost(self):
        table = self.reserve(3)
        with sqlite3.connect(self.service.db_path) as conn:
            reservation_before = conn.execute(f'SELECT * FROM {table}').fetchall()
        before = self.balances()
        request = self.preview()
        self.assertEqual(request['state'], 'ready')
        self.assertEqual(self.balances(), before)
        posted = self.service.post(request['request_id'])
        self.assertIn(posted['state'], {'posted','complete'})
        with localcontext() as context:
            context.prec = 160
            amount = Decimal(canonical_decimal_ratio_text(self.capital,self.quantity))*11
            after = self.balances()
            self.assertEqual(after['FBS'], (86, self.capital-amount))
            self.assertEqual(after['FBO'], (18, Decimal('800.50')+amount))
            self.assertEqual(sum(c for q,c in after.values()), sum(c for q,c in before.values()))
        with sqlite3.connect(self.service.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual([tuple(r) for r in conn.execute(f'SELECT * FROM {table}')], reservation_before)
            document = dict(conn.execute(f'SELECT * FROM {docs.DOCUMENTS_TABLE}').fetchone())
            lines = [dict(r) for r in conn.execute(f'SELECT * FROM {docs.DOCUMENT_LINES_TABLE}')]
            movements = [dict(r) for r in conn.execute(f'SELECT * FROM {docs.LINES_TABLE} ORDER BY line_no')]
            _verify_typed_lines(document, _verified_manifest(document), lines, movements)
            self.assertEqual(Decimal(lines[0]['capital_rub']), amount)
            self.assertEqual([Decimal(m['capital_delta_rub']) for m in movements], [amount.copy_negate(),amount])
            changed = deepcopy(movements); changed[1]['capital_delta_rub'] = '1'
            with self.assertRaises(docs.FfPoolDocumentError):
                _verify_typed_lines(document, _verified_manifest(document), lines, changed)
        self.assertEqual(self.service.post(request['request_id'])['state'], posted['state'])
        self.assertEqual(self.balances(), after)
        with sqlite3.connect(self.service.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {docs.DOCUMENTS_TABLE}').fetchone()[0],1)

    def test_zero_destination_and_last_unit_then_full_drain_preserve_total(self):
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(f"UPDATE {BALANCES_TABLE} SET quantity=0,capital_rub='0',wac_rub=NULL WHERE pool='FBO'")
        for name,q in [('one-left',96),('last-unit',1)]:
            request = self.preview(name,q)
            self.assertEqual(request['state'],'ready')
            self.service.post(request['request_id'])
        self.assertEqual(self.balances(), {'FBS':(0,Decimal(0)),'FBO':(97,self.capital)})

    def test_late_reservation_rejects_atomically(self):
        request = self.preview(q=96)
        self.reserve(3)
        before = self.balances()
        result = self.service.post(request['request_id'])
        self.assertEqual(result['state'],'blocked')
        self.assertEqual(result['error']['code'],'reserved_stock_unavailable')
        self.assertEqual(self.balances(),before)
        with sqlite3.connect(self.service.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {docs.DOCUMENTS_TABLE}').fetchone()[0],0)

    def test_amount_and_baseline_are_typed_and_source_drift_is_rejected(self):
        document = self.plan()['documents'][0]
        docs._validate_exact_pool_transfer(document)
        source = document['movements'][0]
        for q,c in [(self.quantity-1,self.capital), (self.quantity,Decimal('12890'))]:
            with self.assertRaises(docs.FfPoolDocumentError) as caught:
                docs._validate_balance_effect(before_quantity=q,before_capital=c,movement=source)
            self.assertEqual(caught.exception.code,'exact_partial_pool_source_changed')
        for target in ['line_amount','paired_amount','source_basis','policy']:
            changed = deepcopy(document)
            if target == 'line_amount': changed['lines'][0]['capital_exact_rub'] = '1'
            elif target == 'paired_amount':
                changed['lines'][0]['capital_exact_rub'] = '1'
                changed['movements'][0]['capital_delta_exact_rub'] = '-1'
                changed['movements'][1]['capital_delta_exact_rub'] = '1'
            elif target == 'source_basis': changed['movements'][0]['metadata']['source_quantity_before'] += 1
            else: changed['movements'][0]['metadata']['capital_policy'] = 'exact_full_pool_transfer_v1'
            with self.subTest(target=target), self.assertRaises(docs.FfPoolDocumentError):
                docs._validate_exact_pool_transfer(changed)
        preview = docs._posting_plan_preview(plan={**self.plan(),'posted_manifest':{}},epoch=1)
        self.assertEqual(Decimal(preview['capital_delta_rub']),0)
        self.assertEqual(preview['quantity_delta_by_pool'],{'FBS':-11,'FBO':11})

    def test_overspend_expenses_and_generic_fractional_money_remain_rejected(self):
        with self.assertRaises(docs.FfPoolDocumentError) as caught:self.plan(self.manifest(98))
        self.assertEqual(caught.exception.code,'insufficient_source_balance')
        manifest = self.manifest(); manifest['expenses'] = [dict(amount_rub='1.00',basis='fixture')]
        with self.assertRaises(docs.FfPoolDocumentError) as caught:self.plan(manifest)
        self.assertEqual(caught.exception.code,'exact_full_pool_transfer_required')
        with self.assertRaises(docs.FfPoolDocumentError) as caught:docs._money_cents('1.001',field='fixture')
        self.assertEqual(caught.exception.code,'money_minor_unit_required')

    def test_fractional_storno_stays_blocked_without_writes(self):
        request = self.preview(); self.service.post(request['request_id'])
        with sqlite3.connect(self.service.db_path) as conn:
            target = conn.execute(f'SELECT document_id FROM {docs.DOCUMENTS_TABLE}').fetchone()[0]
        before = self.balances()
        reverse = self.service.accept_preview(identity=identity('storno'),document_kind='storno',manifest={'target_document_id':target})
        reverse = self.service.post(reverse['request_id'])
        self.assertEqual(reverse['state'],'blocked')
        self.assertEqual(reverse['error']['code'],'money_minor_unit_required')
        self.assertEqual(self.balances(),before)


if __name__=='__main__':unittest.main()
