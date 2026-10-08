"""Actual native financial source -> supplier/CNY queues -> native publishers."""
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
from datetime import date
from unittest.mock import patch
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_processing_smoke import seed,prepare_source,publish_functional,publish_accounting,NOW,MOMENT,DAY
from apps.operator_supplier_financial_native_smoke import execute,document
from apps.wb_finance_weekly_cost_cutover_smoke import _row
from packages.application import operator_supplier_financial as financial,operator_supplier_shipments as source,operator_supplier_processing as processing,supplier_preparation_intents as intents
from packages.application.operator_supplier_cost_proof import read_native_proof
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def main():
    with TemporaryDirectory(prefix='financial-publication-') as raw:
        rt=seed(raw);prepare_source(rt)
        result=execute(rt,'financial-cost-publication',[{'child_key':'expense','kind':'financial','subject_id':'extra-expense'}],lambda c:document(rt,c))
        child=result['results'][0]['acceptance'];assert not child['processing']['complete']
        with closing(source.readonly(rt.db_path)) as conn:
            identity=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(child['operation_id'],)).fetchone()[0]
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source'])
        assert financial.read_request(rt.runtime_dir,rt.db_path,'financial-cost-publication',request_scope='alice-key')['acceptance']['state']!='completed'
        publish_functional(rt);publish_accounting(rt,opening=True)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
        finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        assert read_native_proof(rt,identity,now=MOMENT)['status']=='verified'
        with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
            assert processing.record_completion(rt,identity,now=MOMENT)['complete']
        receipt=financial.read_request(rt.runtime_dir,rt.db_path,'financial-cost-publication',request_scope='alice-key')['results'][0]['acceptance']
        assert receipt['state']=='completed' and receipt['processing']['complete']
        assert len(rt.list_supplier_financial_expense_lines('source'))==2
        assert receipt['processing']['scopes'][0]['calculation']['expenses_complete'] is False
    print('Actual financial source exact native supplier intent/functional/accounting/ready/Finance completion; other expense preserved; no flag-only completion: OK')

def multiple_cny_native_publication():
    from apps.operator_supplier_financial_native_smoke import setup
    from apps.cny_ledger_smoke import _save_payment
    from packages.application import cny_preparation_intents as cny
    with TemporaryDirectory(prefix='financial-multiple-payments-') as raw:
        rt,ledger=setup(raw)
        rt.save_supplier_financial_document(document=dict(document_id='expense',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=DAY,parse_status='confirmed',total_amount_rub=120),expense_lines=[dict(line_id='expense',amount=120,amount_rub=120,currency='RUB',category='domestic_transport',status='confirmed')])
        receipts=[]
        for key,amount in (('p20','20'),('p80','80')):
            result=execute(rt,'financial-multiple-'+key,[{'child_key':key,'kind':'cny','subject_id':key}],lambda _,key=key,amount=amount:(_save_payment(rt,key,'source',NOW,amount) or {'document_id':key}),lambda c,r:ledger.replay_ledger(reason='multiple-native'))
            receipts.append(result['results'][0]['acceptance'])
        assert all(r['financial_applied'] for r in receipts)
        assert rt.load_cny_ledger_replay_state()['balance_cny']=='0'
        assert cny.read_account_request(rt)['status']!='delivered'
        outcome=cny.drain_cny_preparation_intents(rt,block=ledger)
        assert cny.read_account_request(rt)['status']=='delivered',outcome
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source'])
        publish_functional(rt);publish_accounting(rt,opening=True)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
        finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
            results=processing.reconcile(rt,now=MOMENT)
        current=financial.read_request(rt.runtime_dir,rt.db_path,'financial-multiple-p80',request_scope='alice-key')['results'][0]['acceptance']
        old=financial.read_request(rt.runtime_dir,rt.db_path,'financial-multiple-p20',request_scope='alice-key')['results'][0]['acceptance']
        assert current['processing']['complete'],(current,results)
        assert not old['processing']['complete'] and old['processing']['terminal'],old
        assert old['financial_applied'] and old['reason_ru']=='Эту версию заменили последующими изменениями.'
        assert len([op for op in rt.list_cny_ledger_operations() if op['operation_type']=='supplier_payment_out'])==2
    print('Two actual partial CNY payments preserve financial totals; native account worker exact queue/functional/ready/Finance completed current revision; coalesced older terminal with retained financial application: OK')


if __name__=='__main__':main();multiple_cny_native_publication()
