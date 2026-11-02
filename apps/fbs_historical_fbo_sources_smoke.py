#!/usr/bin/env python3
"""Native commit -> verified typed sources -> unchanged daily solver, LOCAL."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_native_smoke as native_tests
from packages.application import fbs_historical_fbo_native as native
from packages.application.fbs_historical_fbo_sources import capture_historical_fbo_documents, SOURCE_CONTRACT
from packages.application.fbs_snapshot_cost_sources import _documents
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.fbs_document_cost import resolve_document_costs, DOCUMENT_POLICY


class HistoricalFboSourceTests(unittest.TestCase):
    setUp=native_tests.NativeHistoricalFboTests.setUp
    apply=native_tests.NativeHistoricalFboTests.apply
    snapshot=native_tests.NativeHistoricalFboTests.snapshot

    def documents(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            return capture_historical_fbo_documents(conn)

    def test_native_four_sources_are_exact_and_main_reader_admits_only_them(self):
        self.apply()
        before=self.snapshot()
        docs=self.documents()
        self.assertEqual(len(docs),4)
        self.assertEqual([d['kind'] for d in docs],['historical_wb_dispatch','pool_reallocation','historical_wb_dispatch','historical_wb_dispatch'])
        for index,doc in enumerate(docs):
            self.assertEqual(doc['historical_fbo_native']['effect_index'],index)
            self.assertEqual(doc['cost_document']['contract'],SOURCE_CONTRACT)
            self.assertEqual(doc['posted_at'],native_tests.STAMP)
            self.assertEqual(doc['cost_document']['expense_lines'],[])
            if doc['kind']=='historical_wb_dispatch':
                for line,movement in zip(doc['cost_document']['lines'],doc['cost_document']['movements']):
                    self.assertEqual(Decimal(line['capital_rub']),Decimal(movement['capital_delta_rub']).copy_negate())
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            self.assertEqual(_documents(conn),docs)
            for doc in docs:
                self.assertEqual(_documents(conn,document_id=doc['document_id']),[doc])
        self.assertEqual(before,self.snapshot())

    def test_unrelated_unmapped_fbo_movement_still_blocks_main_reader(self):
        self.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f'INSERT INTO {native.OPERATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('unrelated','historical_wb_dispatch','another_system','source','unrelated','unrelated',1,'2026-10-23',native_tests.STAMP,'{}'))
            conn.execute(f'INSERT INTO {native.LINES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',('unrelated',1,'synthetic-moscow','FBO',1,-1,'-50','50','{}'))
            conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            self.assertEqual(len(capture_historical_fbo_documents(conn)),4)
            with self.assertRaisesRegex(ValueError,'unmapped_fbo_document_movement'):_documents(conn)

    def test_corrupt_cohort_cannot_whitelist_unmapped_movement(self):
        self.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            first=native.operation_id(self.plan,0)
            row=conn.execute(f'SELECT metadata_json FROM {native.OPERATIONS_TABLE} WHERE operation_id=?',(first,)).fetchone()
            metadata=json.loads(row[0]);metadata['plan_manifest']['effects'][0]['pool_movements'][0]['capital_delta_rub']='-1'
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                conn.execute(f'UPDATE {native.OPERATIONS_TABLE} SET metadata_json=? WHERE operation_id=?',(json.dumps(metadata),first))
            # Disposable fixture corruption after first proving the live schema
            # prevents this update; the reader must also detect damaged bytes.
            conn.execute('DROP TRIGGER warehouse_business_operations_no_update')
            conn.execute(f'UPDATE {native.OPERATIONS_TABLE} SET metadata_json=? WHERE operation_id=?',(json.dumps(metadata),first));conn.commit()
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'source_plan_invalid'):_documents(conn)

    def test_existing_solver_reprices_typed_days_to_exact_frozen_native(self):
        self.apply();docs=self.documents()
        auxiliary=dict(policy=DOCUMENT_POLICY,fbo_rows=deepcopy(self.capture['opening']['fbo_rows']),transfers={})
        opening=self.capture['opening']['fbs_reallocation_row']
        for day in sorted({d['business_date'] for d in docs}):
            day_docs=[d for d in docs if d['business_date']==day]
            _,auxiliary,valuation=resolve_document_costs(
                {'baseline':{'document_cost_state':auxiliary},'periods':{}},
                {'business_date':day,'documents':day_docs,'quantity_snapshot':{'rows':[]}},
                {'synthetic-moscow:1':opening},{})
            expected=[line for e in self.plan['effects'] if e['business_date']==day and e['kind']=='historical_wb_dispatch' for line in e['pool_movements']]
            actual=valuation.get('historical_fbo_outgoings',[])
            self.assertEqual(len(actual),len(expected))
            for a,e in zip(actual,expected):
                for field in ('source_key','supply_id','business_date','facility_id','pool','nm_id','quantity_delta','capital_delta_rub','wac_rub'):
                    self.assertEqual(a[field],e[field],(day,field))
        # Native document hashes intentionally replace draft hashes; every
        # numeric operand remains equal while source provenance is renewed.
        self.assertEqual(set(auxiliary['fbo_rows']),set(self.plan['fbo_rows_after']))
        for key,row in auxiliary['fbo_rows'].items():
            expected=self.plan['fbo_rows_after'][key]
            for field in ('facility_id','nm_id','quantity','capital_rub','wac_rub'):
                self.assertEqual(row[field],expected[field],field)
            self.assertEqual(row['source']['policy'],expected['source']['policy'])
            self.assertEqual(row['source']['business_date'],expected['source']['business_date'])


if __name__=='__main__':unittest.main()
