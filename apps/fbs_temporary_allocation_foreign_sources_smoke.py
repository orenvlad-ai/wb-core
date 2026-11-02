#!/usr/bin/env python3
"""Exact dated foreign money, immutable replay custody, and fail-closed guards."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import fbs_temporary_allocation_foreign_sources as foreign
from packages.application import warehouse_targeted_replay as targeted
from packages.application.fbs_snapshot_cost import canonical, fingerprint


def source(shipment, quantity, capital, *, day='', fp='source', calc='calculation'):
    return dict(shipment_id=shipment, supplier_flow_id='flow:' + shipment, invoice_no=shipment,
                flow_quantity=str(quantity), flow_capital_rub=str(capital), business_date=day,
                actual_shipment_date=day, source_fingerprint=fp, calculation_fingerprint=calc,
                quality='confirmed_payments_provisional_expenses', expenses_complete_certification=False,
                cost_blockers=[])


def row(stage, nm, q, c, *, records=None):
    return dict(warehouse_key=stage, nm_id=nm, quantity=str(q), capital_rub=str(c),
                wac_rub=str(Decimal(c) / Decimal(q)) if Decimal(q) else None,
                cost_covered_quantity=str(q), quality='confirmed_payments_provisional_expenses', certified=0,
                wb_quantity='0', wb_in_way_to_client='0', wb_in_way_from_client='0',
                provenance=dict(source_records=records or []))


class ForeignSourcesTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:'); self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.day = getattr(self,'REPLAY_DAY','2026-10-31'); self.version = 'targeted:870'
        self.receipt = dict(document_id='receipt724', kind='china_acceptance', source_type='china_acceptance_form',
            source_id='724', business_date='2026-10-26', posted_at='2026-11-01T16:00:00Z',
            cost_document=dict(lines=[dict(quantity=4, line_role='accepted_pool_allocation')],
                movements=[dict(facility_id='moscow', pool='FBO', nm_id=10, quantity_delta=4, capital_delta_rub='40')]))
        self.fee = dict(document_id='fee', kind='pool_overhead', business_date='2026-11-01',
            posted_at='2026-11-01T18:00:00Z', cost_document=dict(movements=[dict(facility_id='moscow', pool='FBO',
                nm_id=10, quantity_delta=0, capital_delta_rub='5')]))
        # Real handoff bridge records intentionally have no kind/source_type.
        self.handoff = dict(document_id='handoff', business_date='2026-10-28', posted_at='2026-11-03T12:00:00Z',
            cost_document=dict(movements=[dict(facility_id='moscow', pool='FBO', nm_id=10,
                quantity_delta=-1, capital_delta_rub='-10')]))
        self.documents = [self.receipt, self.fee, self.handoff]
        old = source('870', 8, 80)
        replacement = source('870', 7, 90, day=self.day)
        self.other = source('other', 3, 33)
        self.before = [row('production', 20, 8, 80, records=[old]), row('production', 21, 3, 33, records=[self.other])]
        self.after = [row('china_to_ff', 20, 7, 90, records=[replacement]), row('production', 21, 3, 33, records=[self.other])]
        self.before_header = dict(shipment_id='870', invoice_no='870', shipment_date='2026-10-31',
            actual_shipment_date=None, actual_ff_acceptance_date=None, order_status='production',
            match_status='ok', expenses_complete=0, updated_at='2026-10-30T12:00:00Z')
        self.after_header = {**self.before_header, 'actual_shipment_date': self.day, 'order_status': 'in_transit'}
        self.current_header = {**self.after_header, 'updated_at': '2026-11-01T17:00:00Z'}
        self.create(foreign.P + 'supplier_shipments', self.current_header)
        cost_columns = {
            'lines': 'line_id,line_type,sort_order,internal_nm_id,qty,unit_price,amount,match_status',
            'documents': 'document_id,document_type,document_date,parse_status,parser_version,updated_at',
            'expenses': 'line_id,financial_document_id,sort_order,category,amount,currency,amount_rub,vat_rate,vat_amount_rub,status,raw_json',
            'cny_operations': 'operation_id,operation_type,operation_date,sequence_key,cny_delta,rub_value_delta,source_document_id,updated_at',
        }
        cost_tables = {'lines': ('supplier_shipment_lines', 'shipment_id'),
            'documents': ('supplier_financial_documents', 'supplier_order_id'),
            'expenses': ('supplier_financial_expense_lines', 'supplier_order_id'),
            'cny_operations': ('cny_ledger_operations', 'source_order_id')}
        for name, (table, binding) in cost_tables.items():
            self.conn.execute(f'CREATE TABLE {foreign.P}{table}({binding},' + cost_columns[name] + ')')
        self.undo = dict(shipment_id='870', before_header=self.before_header, after_header=self.after_header,
            base_version_id='base', published_version_id=self.version, target_rows_before=self.before, target_rows_after=self.after)
        revision = targeted._fingerprint(dict(header=self.before_header, lines=[], documents=[], expenses=[], cny_operations=[]))
        self.publication = dict(publication_id='pub', version_id=self.version, stable_source_id='supplier_shipment:870',
            source_revision=revision, earliest_business_date=self.day, affected_nm_ids_json='[20,21]', base_version_id='base',
            plan_fingerprint='plan', target_before_digest=targeted._fingerprint(self.before),
            target_after_digest=targeted._fingerprint(self.after), non_target_before_digest=targeted._fingerprint([]),
            non_target_after_digest=targeted._fingerprint([]), status='complete', blocker_summary_json='[]', completed_at='2026-11-01T17:00:00Z')
        self.create(targeted.TARGETED_PUBLICATION_TABLE, self.publication)
        self.create(targeted.TARGETED_UNDO_TABLE, dict(publication_id='pub', shipment_id='870',
            before_header_json=canonical(self.before_header), after_header_json=canonical(self.after_header),
            base_version_id='base', published_version_id=self.version, target_rows_before_json=canonical(self.before),
            target_rows_after_json=canonical(self.after), manifest_digest=targeted._fingerprint(self.undo), status='ready', rolled_back_at=None))
        self.create(foreign.P + 'warehouse_functional_versions', dict(version_id=self.version, status='good',
            cutover_id=foreign.FUNCTIONAL_CUTOVER_ID,
            version_kind='targeted_supplier_replay', business_effective_date=self.day, effective_at='2026-11-01T17:00:00Z',
            created_at='2026-11-01T17:00:00Z', plan_fingerprint='plan', source_watermarks_json=canonical(dict(
                base_version_id='base', stable_source_id='supplier_shipment:870', source_revision=revision, earliest_business_date=self.day))))
        for r in self.after:
            self.create(foreign.P + 'warehouse_functional_balances', {**{k:v for k,v in r.items() if k != 'provenance'},
                'version_id': self.version, 'provenance_json': canonical(r['provenance'])})
        self.state = dict(version_id=self.version, shipment_id='870', source_fingerprint='source',
            calculation_fingerprint='calculation', expenses_complete=0, calculation_available=1)
        self.create(foreign.P + 'warehouse_supplier_cost_states', self.state)
        for d in self.documents:
            self.create(foreign.P + 'warehouse_business_operations', dict(operation_id=d['document_id'],
                business_date=d['business_date'], posted_at=d['posted_at']))
        self.saved = {d: self.saved_day(d) for d in ['2026-10-27','2026-10-31','2026-11-02']}
        self.conn.commit(); self.conn.execute('PRAGMA query_only=ON'); self.conn.execute('BEGIN')

    def create(self, table, values):
        columns = list(values)
        self.conn.execute(f'CREATE TABLE IF NOT EXISTS {table}(' + ','.join('"'+k+'"' for k in columns) + ')')
        self.conn.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})', list(values.values()))

    def saved_day(self, day):
        native = day >= '2026-11-02'
        q, c = (14, '145.00000000000000000000000000000000000000000000000000000000000001') if native else (10, '100.00000000000000000000000000000000000000000000000000000000000001')
        ff = row('ff', 10, q, c, records=[dict(locations=[dict(facility_id='moscow', pool='FBO', quantity=str(q), capital_rub=c)])])
        supplier = deepcopy(self.after if native else self.before)
        if not native: supplier.append(row('china_to_ff', 10, 4, 40, records=[source('724', 4, 40)]))
        return dict(version=dict(version_id='dated:' + day, source_watermarks_json=canonical(dict(captured_at=day+'T12:00:00Z'))),
            balances=sorted([ff, row('wb', 99, 7, 77, records=[dict(official='true dated snapshot')]), *supplier],
                key=lambda r:(r['warehouse_key'],r['nm_id'])),
            companions=dict(supplier_cost_states=[deepcopy(self.state)], unmatched_doprinato=[dict(reason='foreign unknown')],
                functional_ff_reservations=[dict(supply_id='foreign', nm_id=99, quantity=7)]))

    def capture(self, docs=None, versions=None):
        return foreign.capture_foreign_suffix_sources(self.conn, saved_by_day=self.saved,
            documents=self.documents if docs is None else docs,
            targeted_version_ids=[self.version] if versions is None else versions,
            date_from=min(self.saved), date_to=max(self.saved))

    def build(self, day, capture=None, docs=None):
        return foreign.rebuild_foreign_suffix_day(self.saved[day], day=day,
            documents=self.documents if docs is None else docs, foreign_capture=capture or self.capture())

    def test_native_late_documents_keep_dates_money_foreign_wb_and_receipt_departure(self):
        capture=self.capture(); original=deepcopy(self.saved)
        before=self.build('2026-10-27',capture); dated=self.build('2026-10-31',capture); later=self.build('2026-11-02',capture)
        self.assertEqual(self.saved,original)
        for result, expected_q, expected_c in [(before,14,140),(dated,13,130),(later,13,135)]:
            ff=next(r for r in result['balances'] if r['warehouse_key']=='ff')
            self.assertEqual(Decimal(ff['quantity']),expected_q)
            self.assertEqual(Decimal(ff['capital_rub']),Decimal(str(expected_c)+'.'+next(r for r in original['2026-10-27']['balances'] if r['warehouse_key']=='ff')['capital_rub'].split('.')[1]))
            self.assertEqual(next(r for r in result['balances'] if r['warehouse_key']=='wb'),next(r for r in original['2026-10-27']['balances'] if r['warehouse_key']=='wb'))
            self.assertFalse(any(s.get('shipment_id')=='724' for r in result['balances'] if r['warehouse_key'] in foreign.SUPPLIER_STAGES for s in r['provenance']['source_records']))
            self.assertEqual(result['companions']['unmatched_doprinato'],[dict(reason='foreign unknown')])
        self.assertTrue(any(r['warehouse_key']=='production' and r['nm_id']==20 for r in before['balances']))
        self.assertEqual(next(r for r in dated['balances'] if r['warehouse_key']=='china_to_ff' and r['nm_id']==20)['capital_rub'],'90')
        self.assertTrue(later['proof']['targeted_rebases'][0]['already_in_native_frontier'])

    def test_duplicate_exact_documents_do_not_repeat_receipts_fees_or_handoffs(self):
        duplicates=self.documents+self.documents
        self.assertEqual(self.capture(duplicates),self.capture())
        self.assertEqual(self.build('2026-10-31',docs=duplicates),self.build('2026-10-31'))

    def test_targeted_native_six_decimal_aggregate_does_not_truncate_source_records(self):
        own=source('870',7,'90');other=source('other',4,'40.00000000000000000000001')
        mixed=row('china_to_ff',20,11,130,records=[own,other]);before=deepcopy(mixed)
        with localcontext() as context:
            context.prec=160
            with self.assertRaisesRegex(ValueError,'supplier_records_not_conserved'):foreign._sources([mixed],'870')
            self.assertEqual(foreign._sources([mixed],'870',targeted_policy=True)[0]['source'],own)
        self.assertEqual(mixed,before)

    def test_cost_drift_header_drift_rollback_and_unknown_selection_fail(self):
        for sql,args,reason in [
            (f"UPDATE {foreign.P}supplier_shipments SET order_status='changed'",(),'supplier_header_changed'),
            (f"INSERT INTO {foreign.P}cny_ledger_operations(source_order_id,operation_id) VALUES('870','newpayment')",(),'supplier_cost_inputs_changed'),
            (f"UPDATE {targeted.TARGETED_UNDO_TABLE} SET status='rolled_back'",(),'replay_not_complete'),
            (f"UPDATE {foreign.P}warehouse_supplier_cost_states SET calculation_fingerprint='other'",(),'replay_cost_state_changed')]:
            with self.subTest(reason=reason):
                self.conn.execute('PRAGMA query_only=OFF');self.conn.execute('SAVEPOINT mutation');self.conn.execute(sql,args);self.conn.execute('PRAGMA query_only=ON')
                with self.assertRaisesRegex(ValueError,reason):self.capture()
                self.conn.execute('PRAGMA query_only=OFF');self.conn.execute('ROLLBACK TO mutation');self.conn.execute('RELEASE mutation');self.conn.execute('PRAGMA query_only=ON')
        with self.assertRaisesRegex(ValueError,'targeted_selection_changed'):self.capture(versions=[self.version,self.version])
        with self.assertRaisesRegex(ValueError,'unknown_late_native_operation'):self.capture(docs=[self.receipt,self.fee])

    def test_missing_or_changed_foreign_source_is_never_zero_or_success(self):
        capture=self.capture();changed=deepcopy(self.saved['2026-10-31']);changed['balances'][0]['quantity']='0'
        with self.assertRaisesRegex(ValueError,'saved_source_changed'):
            foreign.rebuild_foreign_suffix_day(changed,day='2026-10-31',documents=self.documents,foreign_capture=capture)
        bad=deepcopy(self.documents);bad[0]['cost_document']['movements'][0]['capital_delta_rub']='0'
        with self.assertRaisesRegex(ValueError,'native_documents_changed'):self.build('2026-10-31',capture,docs=bad)

    def test_unknown_foreign_supplier_cost_and_later_precision_are_preserved(self):
        unknown=source('unknown',3,0);unknown.update(quality='cost_unavailable',cost_freshness='unavailable')
        unavailable=row('production',777,3,0,records=[unknown]);unavailable.update(wac_rub=None,cost_covered_quantity='0')
        for saved in self.saved.values():
            saved['balances'].append(deepcopy(unavailable));saved['balances'].sort(key=lambda r:(r['warehouse_key'],r['nm_id']))
        later=next(r for r in self.saved['2026-11-02']['balances'] if r['warehouse_key']=='china_to_ff' and r['nm_id']==20)
        later['capital_rub']='90.0000000000000000000000001';later['provenance']['source_records'][0]['flow_capital_rub']=later['capital_rub']
        capture=self.capture()
        for day in self.saved:
            output=self.build(day,capture)
            self.assertEqual(next(r for r in output['balances'] if r['nm_id']==777),unavailable)
        self.assertEqual(next(r for r in self.build('2026-11-02',capture)['balances'] if r['warehouse_key']=='china_to_ff' and r['nm_id']==20)['capital_rub'],later['capital_rub'])

    def test_fenced_inputs_exclude_new_owned_operations_and_allow_earlier_effective_replay(self):
        capture=self.capture()
        self.assertTrue(capture['query_fence'])
        self.assertFalse(any('warehouse_business_operations' in sql for sql,_,_ in capture['query_fence']))
        fixture=ForeignSourcesTests(methodName='runTest');fixture.REPLAY_DAY='2026-10-25';fixture.setUp();self.addCleanup(fixture.doCleanups)
        earlier=fixture.capture();self.assertEqual(earlier['replays'][0]['business_date'],'2026-10-25')
        self.assertTrue(any(r['warehouse_key']=='china_to_ff' and r['nm_id']==20 for r in fixture.build('2026-10-27',earlier)['balances']))

    def test_fractional_and_submillisecond_late_unknown_operations_cannot_hide_at_frontier(self):
        for timestamp in ('2026-10-27T12:00:00.1Z','2026-10-27T12:00:00.000001Z',
                          '2026-10-27T15:00:00.000001+03:00'):
            with self.subTest(timestamp=timestamp):
                self.conn.execute('PRAGMA query_only=OFF');self.conn.execute('SAVEPOINT fractional')
                self.create(foreign.P+'warehouse_business_operations',dict(operation_id='unknown',business_date='2026-10-27',posted_at=timestamp))
                self.conn.execute('PRAGMA query_only=ON')
                with self.assertRaisesRegex(ValueError,'unknown_late_native_operation'):self.capture()
                self.conn.execute('PRAGMA query_only=OFF');self.conn.execute('ROLLBACK TO fractional');self.conn.execute('RELEASE fractional');self.conn.execute('PRAGMA query_only=ON')
        self.conn.execute('PRAGMA query_only=OFF')
        self.create(foreign.P+'warehouse_business_operations',dict(operation_id='equal',business_date='2026-10-27',posted_at='2026-10-27T15:00:00+03:00'))
        self.conn.execute('PRAGMA query_only=ON')
        self.assertNotIn('equal',{r['operation_id'] for r in self.capture()['late_native_operations']})

    def test_invalid_native_timestamps_fail_and_frontiers_are_compared_in_utc(self):
        for timestamp in ('invalid','2026-10-27T12:00:00','2026-10-27T12:00:00.0000001Z'):
            with self.subTest(timestamp=timestamp):
                self.conn.execute('PRAGMA query_only=OFF');self.conn.execute('SAVEPOINT invalid')
                self.create(foreign.P+'warehouse_business_operations',dict(operation_id='unknown',business_date='2026-10-27',posted_at=timestamp))
                self.conn.execute('PRAGMA query_only=ON')
                with self.assertRaisesRegex(ValueError,'native_timestamp_invalid'):self.capture()
                self.conn.execute('PRAGMA query_only=OFF');self.conn.execute('ROLLBACK TO invalid');self.conn.execute('RELEASE invalid');self.conn.execute('PRAGMA query_only=ON')
        self.saved['2026-10-27']['version']['source_watermarks_json']=canonical(dict(captured_at='2026-10-27T14:00:00+03:00'))
        self.conn.execute('PRAGMA query_only=OFF')
        self.create(foreign.P+'warehouse_business_operations',dict(operation_id='utc-late',business_date='2026-10-27',posted_at='2026-10-27T12:00:00Z'))
        self.conn.execute('PRAGMA query_only=ON')
        with self.assertRaisesRegex(ValueError,'unknown_late_native_operation'):self.capture()

    def test_native_non_target_digest_is_recomputed_even_if_stored_before_after_agree(self):
        self.conn.execute('PRAGMA query_only=OFF')
        self.conn.execute(f"UPDATE {targeted.TARGETED_PUBLICATION_TABLE} SET non_target_before_digest='agreed-but-false',non_target_after_digest='agreed-but-false'")
        self.conn.execute('PRAGMA query_only=ON')
        with self.assertRaisesRegex(ValueError,'replay_native_foreign_rows_changed'):self.capture()

    def test_native_replay_may_not_change_a_foreign_source_even_with_new_self_consistent_hashes(self):
        changed=deepcopy(self.undo);changed['target_rows_after']=deepcopy(self.after);other=changed['target_rows_after'][1]
        other['quantity']='4';other['capital_rub']='44';other['provenance']['source_records'][0].update(flow_quantity='4',flow_capital_rub='44')
        self.conn.execute('PRAGMA query_only=OFF')
        self.conn.execute(f'UPDATE {targeted.TARGETED_UNDO_TABLE} SET target_rows_after_json=?,manifest_digest=?',
            (canonical(changed['target_rows_after']),targeted._fingerprint(changed)))
        self.conn.execute(f'UPDATE {targeted.TARGETED_PUBLICATION_TABLE} SET target_after_digest=?',
            (targeted._fingerprint(changed['target_rows_after']),))
        self.conn.execute('PRAGMA query_only=ON')
        with self.assertRaisesRegex(ValueError,'replay_changed_foreign_sources'):self.capture()

    def test_no_targeted_replay_is_supported_and_does_not_require_supplier_tables(self):
        self.conn.execute('PRAGMA query_only=OFF');self.conn.execute(f'DELETE FROM {foreign.P}warehouse_functional_versions');self.conn.execute('PRAGMA query_only=ON')
        self.conn.execute('PRAGMA query_only=OFF')
        for table in ('supplier_shipments','supplier_shipment_lines','supplier_financial_documents','supplier_financial_expense_lines','cny_ledger_operations'):
            self.conn.execute(f'DROP TABLE {foreign.P}{table}')
        self.conn.execute('PRAGMA query_only=ON')
        capture=self.capture(versions=[])
        result=self.build('2026-10-31',capture)
        self.assertEqual(result['proof']['targeted_rebases'],[])
        self.assertTrue(any(r['warehouse_key']=='production' and r['nm_id']==20 for r in result['balances']))


if __name__=='__main__':
    unittest.main()
