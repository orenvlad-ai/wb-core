"""Actual supplier preparation and dated native component dependency guards."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import sys, unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_processing_smoke import seed,prepare_source,publish_functional,publish_accounting,edit,DAY,MOMENT
from packages.application import operator_supplier_shipments as source,supplier_preparation_intents as intents
from packages.application.operator_supplier_cost_proof import numerical_supplier_state
from packages.application import operator_supplier_history_sources as h


class NativeDependencies(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='supplier-history-native-');self.addCleanup(self.temp.cleanup)
        self.runtime=seed(self.temp.name);self.header,self.lines=prepare_source(self.runtime)
        self.assertEqual(intents.drain_supplier_preparation_intents(self.runtime,shipment_ids=['source'])['status'],'queued')
        self.publication=publish_functional(self.runtime)
    def manifest(self):
        with closing(source.readonly(self.runtime.db_path)) as conn:
            state,allocation=numerical_supplier_state(conn,'source')
            self.assertEqual(state['calculation_available'],1)
            return h.component_manifest(conn,shipment_id='source',allocation=allocation)
    def rows(self):
        with closing(source.readonly(self.runtime.db_path)) as conn:
            rows=[]
            for r in conn.execute('SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id',(self.publication['active_version']['version_id'],)):
                row=dict(r);row['provenance']=__import__('json').loads(row.pop('provenance_json'));rows.append(row)
            return rows
    def test_actual_prepared_source_and_dated_cost_revision(self):
        manifest=self.manifest();before=self.rows()
        after,proof=h.dated_supplier_rows(before,shipment=manifest,day=DAY)
        money=lambda rows:sum((Decimal(r['capital_rub']) for r in rows if r['warehouse_key']=='production'),Decimal(0))
        self.assertEqual(money(after),money(before));self.assertEqual(proof['kind'],'native_dated_supplier_components')
        changed=deepcopy(manifest);component=changed['lines'][0]['current_components'][-1]
        component['amount_rub']=str(Decimal(str(component['amount_rub']))+100)
        revised,_=h.dated_supplier_rows(before,shipment=changed,day=DAY)
        self.assertEqual(money(revised)-money(before),100)
        self.assertEqual([(r['warehouse_key'],r['nm_id'],r['quantity']) for r in after],[(r['warehouse_key'],r['nm_id'],r['quantity']) for r in revised])
    def test_no_current_destination_erases_prior_source(self):
        manifest=self.manifest();manifest['actual_ff_acceptance_date']='2026-07-25'
        after,proof=h.dated_supplier_rows(self.rows(),shipment=manifest,day=DAY)
        self.assertEqual(proof['kind'],'native_dated_supplier_components')
        self.assertTrue(any(r['warehouse_key']=='production' for r in after))
    def test_foreign_quantity_or_missing_component_date_fail_closed(self):
        manifest=self.manifest();manifest['lines'][0]['quantity']='999'
        with self.assertRaisesRegex(ValueError,'supplier_physical_quantity_changed'):
            h.dated_supplier_rows(self.rows(),shipment=manifest,day=DAY)
    def test_actual_unaccepted_relation_is_not_frozen_receipt(self):
        with closing(source.readonly(self.runtime.db_path)) as conn:
            self.assertEqual(h.receipt_dependency(conn,'source')['kind'],'unaccepted')
    def legacy_prefix(self):
        import json
        from packages.application.registry_upload_db_backed_runtime import _connect
        from packages.application import fbs_accounting_historical_stages as stages
        self.runtime.save_supplier_shipment(header={**self.header,'actual_shipment_date':DAY,'actual_ff_acceptance_date':DAY},lines=self.lines)
        self.runtime.save_supplier_financial_document(document=dict(document_id='extra',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=DAY+'T09:00:00Z',updated_at=DAY+'T09:00:00Z',document_date=DAY,parse_status='confirmed',total_amount_rub=120),expense_lines=[dict(line_id='extra-line',amount=120,amount_rub=120,currency='RUB',category='logistics',status='confirmed')])
        with _connect(self.runtime.db_path) as conn:
            for identity,kind,sid,q,stamp in [('legacy-in','supplier_shipment','source',10,DAY+'T09:00:00Z'),('legacy-out','wb_supply','supply',-5,DAY+'T09:01:00Z')]:
                conn.execute('INSERT INTO sheet_vitrina_v1_ff_stock_operations(operation_id,operation_type,source_type,source_key,source_object_id,created_at,business_effective_date) VALUES(?,?,?,?,?,?,?)',(identity,'fixture',kind,identity,sid,stamp,DAY))
                conn.execute('INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(operation_id,line_no,nm_id,quantity_delta,raw_json) VALUES(?,1,1,?,?)',(identity,q,'{}'))
            conn.commit()
        with closing(source.readonly(self.runtime.db_path)) as conn:
            version=dict(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id='base'").fetchone())
            return dict(version=version,balances=[],companions={'supplier_cost_states':[]}),self.manifest(),h.receipt_dependency(conn,'source')
    def test_actual_legacy_prefix_ff_and_native_transit_money(self):
        saved,manifest,dependency=self.legacy_prefix();saved['version']['published_at']=DAY+'T10:00:00Z'
        base=dict(version_id='base',nm_id=1,cost_covered_quantity='35',quality='moving_weighted_average',certified=0,wb_quantity='0',wb_in_way_to_client='0',wb_in_way_from_client='0')
        saved['balances']=[{**base,'warehouse_key':'ff','quantity':'35','capital_rub':'1137.5','wac_rub':'32.5','provenance':{'source_records':[]}},
            {**base,'warehouse_key':'ff_to_wb','quantity':'5','capital_rub':'177.5','wac_rub':'35.5','cost_covered_quantity':'5','provenance':{'source_records':[dict(supply_id='supply',flow_quantity='5',flow_capital_rub='177.5',ff_wac_at_ledger_debit_rub='32.5',downstream_pre_acceptance_addon_rub='3')]}}]
        with closing(source.readonly(self.runtime.db_path)) as conn:after,proof,pins=h.dated_ff_rows(conn,saved,shipment=manifest,dependency=dependency,day=DAY)
        self.assertEqual([Decimal(r['quantity']) for r in after],[Decimal(35),Decimal(5)])
        self.assertEqual([Decimal(r['capital_rub']) for r in after],[Decimal('1417.5'),Decimal('217.5')])
        self.assertEqual(saved['balances'][0]['capital_rub'],'1137.5');self.assertTrue(pins)
    def test_native_discrepancy_reconciliation_preserves_matches(self):
        saved,manifest,dependency=self.legacy_prefix();saved['version']['published_at']=DAY+'T10:00:00Z'
        receipt=dict(source_id='supply:1',source_fingerprint='saved',business_date=DAY,nm_id=1,quantity='2',capital='71',wac='35.5',cost_covered_quantity='2',provenance=dict(supply_id='supply',ff_wac_at_ledger_debit_rub='32.5',downstream_pre_acceptance_addon_rub='3',paid_acceptance_excluded=True))
        from packages.application.warehouse_functional import reconcile_discrepancies
        pools,_=reconcile_discrepancies(discrepancies=[receipt],doprinato=[dict(source_id='dop:1',business_date=DAY,nm_id=1,quantity='1')])
        saved['balances']=[dict(warehouse_key='wb_acceptance_discrepancy',nm_id=1,quantity='1',capital_rub='35.5',wac_rub='35.5',cost_covered_quantity='1',provenance=dict(receipts=[receipt],doprinato_matches=pools[0]['matches']))]
        with closing(source.readonly(self.runtime.db_path)) as conn:after,_,_=h.dated_ff_rows(conn,saved,shipment=manifest,dependency=dependency,day=DAY)
        self.assertEqual(after[0]['quantity'],'1');self.assertEqual(after[0]['capital_rub'],'43.5');self.assertEqual(after[0]['provenance']['doprinato_matches'][0]['matched_quantity'],'1')
        self.assertEqual(saved['balances'][0]['capital_rub'],'35.5')
    def test_actual_native_publisher_can_prove_cost_no_change(self):
        from packages.application import operator_supplier_history_candidate as candidate
        accepted=edit(self.runtime,self.header,self.lines,'exact-cost-no-change','same-money-new-label')
        identity=accepted['acceptance']['operation_id']
        self.assertEqual(intents.drain_supplier_preparation_intents(self.runtime,shipment_ids=['source'])['status'],'queued')
        publish_functional(self.runtime);publish_accounting(self.runtime,opening=True)
        result=candidate.prepare(self.runtime,identity,now=MOMENT)
        self.assertEqual(result['effect_dates'],[])
        self.assertEqual(result['current_evaluation']['before_digest'],result['current_evaluation']['after_digest'])
        self.assertEqual(candidate.validate(result,runtime=self.runtime,now=MOMENT),result)
        self.assertEqual(result['editions'],{})
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application import operator_supplier_processing as processing
        WbFinanceWeeklyBlock(self.runtime.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT).ensure_schema()
        with heavy_admitted(self.runtime.runtime_dir,operation='fixture'),warehouse_functional_job_lock(self.runtime.runtime_dir):
            self.assertTrue(processing.record_completion(self.runtime,identity,now=MOMENT)['complete'])
        receipt=source.read_acceptance(self.runtime.db_path,identity,request_scope='alice-key')
        self.assertEqual(receipt['state'],'completed')
        self.assertEqual(receipt['processing']['kind'],'derived_no_change')
        self.assertEqual(receipt['reason_code'],'cost_not_changed')
        self.assertNotIn('history_complete',receipt['processing'])
        self.assertNotIn('six_stages_applied',receipt['processing'])


class FrozenReceiptDependencies(unittest.TestCase):
    def test_actual_modern_receipt_keeps_posted_money_and_exact_relation(self):
        from apps import warehouse_ff_acceptance_form_smoke as guided
        from packages.application.ff_pool_documents import FfPoolDocumentService
        from packages.application.registry_upload_db_backed_runtime import _connect
        with TemporaryDirectory(prefix='supplier-history-frozen-') as raw:
            rt,surface=guided._fixture(Path(raw));payload=guided._payload(surface);payload['business_date']=guided.DAY
            preview=surface.accept_china_form(payload,actor='synthetic dependency')
            service=FfPoolDocumentService(db_path=rt.db_path,runtime_dir=rt.runtime_dir,resume=False,timestamp_factory=lambda:guided.NOW)
            posted=service.post(preview['request_id'],defer_replay=True)
            self.assertEqual(posted['state'],'posted')
            with closing(source.readonly(rt.db_path)) as conn:
                with self.assertRaisesRegex(ValueError,'frozen_relation_missing'):h.receipt_dependency(conn,guided.SHIPMENT)
            from packages.application.business_data_heavy_admission import heavy_admitted
            from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
            with heavy_admitted(rt.runtime_dir,operation='cycle'),warehouse_functional_job_lock(rt.runtime_dir):
                service.post(preview['request_id'],defer_replay=False)
            with closing(source.readonly(rt.db_path)) as conn:
                dep=h.receipt_dependency(conn,guided.SHIPMENT)
                self.assertEqual(dep['kind'],'immutable_posted_supplier_receipt')
                saved={'balances':[{'warehouse_key':'ff','nm_id':guided.NM_ID,'quantity':'10','capital_rub':'1000','provenance':{}}]}
                unchanged,proof,pins=h.dated_ff_rows(conn,saved,shipment={'shipment_id':guided.SHIPMENT},dependency=dep,day=guided.DAY)
                self.assertEqual(unchanged,saved['balances']);self.assertEqual(pins,[])
                original=dep['operations'][0]['operation_id']
            # A real different connection changes only the legacy alias; it
            # cannot grant a foreign supplier the posted native document.
            with _connect(rt.db_path) as conn:conn.execute('UPDATE sheet_vitrina_v1_ff_stock_operations SET source_object_id=? WHERE operation_id=?',('foreign',original));conn.commit()
            with closing(source.readonly(rt.db_path)) as conn:
                with self.assertRaisesRegex(ValueError,'frozen_relation_missing'):h.receipt_dependency(conn,'foreign')


if __name__=='__main__':unittest.main()
