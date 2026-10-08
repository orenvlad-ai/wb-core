"""Actual CNY source -> account consumer -> functional/accounting/Finance proof."""
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup
from apps.operator_supplier_processing_smoke import publish_functional,publish_accounting,NOW,MOMENT,DAY
from apps.cny_ledger_smoke import _save_payment
from apps.wb_finance_weekly_cost_cutover_smoke import _row
from packages.application import operator_cny_documents as cny,operator_supplier_processing as processing
from packages.application import cny_preparation_intents as intents,supplier_preparation_intents as supplier
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def main():
    with TemporaryDirectory(prefix='operator-cny-publication-') as raw:
        rt,block=setup(raw)
        rt.save_supplier_financial_document(document=dict(document_id='expense',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=DAY,parse_status='confirmed',total_amount_rub=120),
            expense_lines=[dict(line_id='expense',amount=120,amount_rub=120,currency='RUB',category='domestic_transport',status='confirmed')])
        for key,amount in [('pay20','20'),('pay80','80')]:
            result=cny.execute(block,action='cny_upload',payload={'request_id':'cny-publication-'+key},actor='alice',request_scope='alice-key',
                native_write=lambda key=key,amount=amount:(_save_payment(rt,key,'source',NOW,amount) or {'document_id':key}))
            assert result['acceptance']['children'][0]['financial_applied'] is True
            assert not result['acceptance']['processing']['complete']
        assert intents.read_account_request(rt)['status']!='delivered'
        intents.drain_cny_preparation_intents(rt,block=block)
        supplier.drain_supplier_preparation_intents(rt,shipment_ids=['source'])
        assert not cny.read_request(rt.runtime_dir,rt.db_path,'cny-publication-pay80',request_scope='alice-key')['acceptance']['processing']['complete']
        publish_functional(rt);publish_accounting(rt,opening=True)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT)
        finance.ensure_schema();finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
            processing.reconcile(rt,now=MOMENT)
        current=cny.read_request(rt.runtime_dir,rt.db_path,'cny-publication-pay80',request_scope='alice-key')['acceptance']['children'][0]
        old=cny.read_request(rt.runtime_dir,rt.db_path,'cny-publication-pay20',request_scope='alice-key')['acceptance']['children'][0]
        assert current['processing']['complete'] and current['financial_applied'],current
        assert current['processing']['scopes'][0]['calculation']['expenses_complete'] is False
        assert not old['processing']['complete'] and old['processing']['terminal'] and old['financial_applied'],old
        assert cny.read_status(block)['financial_authority']['current']
        assert rt.load_cny_ledger_replay_state()['balance_cny']=='0'
    print('Actual CNY account/functional/accounting/ready/Finance exact completion; provisional expenses separate; old revision terminal; no delivered-only completion: OK')


if __name__=='__main__':main()
