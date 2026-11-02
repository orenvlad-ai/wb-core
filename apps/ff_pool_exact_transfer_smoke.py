#!/usr/bin/env python3
"""Local exact-capital full-pool transfer; no synthetic monetary rounding."""
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

    def test_partial_or_expensed_fractional_capital_is_not_rounded(self):
        for mode in ('partial','expense'):
            manifest=self.manifest()
            if mode=='partial':manifest['items'][1]['quantity']-=1
            else:manifest['expenses']=[dict(amount_rub='1.00',basis='test')]
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

if __name__=='__main__':unittest.main()
