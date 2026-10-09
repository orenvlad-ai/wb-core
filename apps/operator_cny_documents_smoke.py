"""Actual direct CNY source, financial authority, restart and domain separation."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup, ledger_business
from apps.cny_ledger_smoke import _save_payment, _fixture_text_extractor
from packages.application import operator_cny_documents as cny, operator_supplier_financial as financial
from packages.application import operator_supplier_shipments as source, cny_preparation_intents as intents
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.registry_upload_db_backed_runtime import _connect, RegistryUploadDbBackedRuntime
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint

SCOPE = 'alice-key'


def action(block, name, identity, write, **kw):
    return cny.execute(block, action='cny_' + name, payload={'request_id': identity, **kw.pop('payload', {})},
        actor='alice', request_scope=SCOPE, native_write=write, **kw)


def main():
    with TemporaryDirectory(prefix='operator-cny-native-') as raw:
        rt, block = setup(raw)
        assert cny.read_status(block)['financial_authority']['current']
        with closing(source.readonly(rt.db_path)) as conn:
            assert conn.execute(f'SELECT count(*) FROM {financial.REQUESTS}').fetchone()[0] == 0
        old = rt.list_cny_documents()[0]
        rt.save_cny_document({**old,'rub_amount':'1100','parsed_payload':{**old['parsed_payload'],'rub_value':'1100'}})
        assert not cny.read_status(block)['financial_authority']['current']
        block.replay_ledger(reason='actual-legacy-only-worker')
        assert cny.read_status(block)['financial_authority']['current']
        opening = {'operation_date':'2026-07-24','cny_amount':'100','rub_value':'1200'}
        with patch.object(OwnProductCapitalBlock, 'recalculate', side_effect=AssertionError('HTTP cost')):
            result = action(block, 'opening', 'cny-opening-saved', lambda: block.create_opening_balance(opening), payload=opening)
        receipt = result['acceptance']; assert receipt['domain'] == cny.DOMAIN
        assert receipt['children'][0]['financial_applied'] is True and receipt['processing']['complete']
        authority = cny.read_status(block)['financial_authority']; assert authority['current']
        before = rt.db_path.read_bytes()
        recovered = cny.read_request(rt.runtime_dir,rt.db_path,'cny-opening-saved',request_scope=SCOPE)
        assert recovered['acceptance']['operation_id'] == receipt['operation_id']
        assert rt.db_path.read_bytes() == before
        assert financial.read_request(rt.runtime_dir,rt.db_path,'cny-opening-saved',request_scope=SCOPE)['status'] == 'unknown'
        assert financial.read_operation(rt.runtime_dir,rt.db_path,receipt['operation_id'],request_scope=SCOPE)['status'] == 'unknown'
        assert cny.read_operation(rt.runtime_dir,rt.db_path,receipt['operation_id'],request_scope='foreign')['status'] == 'unknown'
        same = action(block, 'opening', 'cny-opening-saved', lambda: (_ for _ in ()).throw(AssertionError('resubmit')), payload=opening)
        assert same['acceptance']['operation_id'] == receipt['operation_id']
        # Opening replacement retains native same-date ID and creates a new action revision.
        newer = action(block, 'opening', 'cny-opening-newer', lambda: block.create_opening_balance({**opening,'rub_value':'1300'}), payload={**opening,'rub_value':'1300'})
        assert newer['results'][0]['document_id'] == result['results'][0]['document_id']
        assert newer['results'][0]['acceptance']['source_ref']['revision'] == 2
        # Real foreign ledger write invalidates current authority despite unchanged source.
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_cny_ledger_operations SET cny_delta='999' WHERE operation_type='opening_balance'");conn.commit()
        assert not cny.read_status(block)['financial_authority']['current']
        block.replay_ledger(reason='native-worker-repair')
        assert cny.read_status(block)['financial_authority']['current']

        failed = action(block,'opening','cny-opening-invalid',lambda:block.create_opening_balance({'operation_date':'bad'}),payload={'operation_date':'bad'})
        assert failed['status']=='rejected' and failed['acceptance'] is None
        assert cny.read_request(rt.runtime_dir,rt.db_path,'cny-opening-invalid',request_scope=SCOPE)['status']=='rejected'
    with TemporaryDirectory(prefix='operator-cny-busy-') as raw:
        rt, block = setup(raw)
        # Source commit and replay interruption are deliberately separate.
        with patch.object(block,'replay_ledger',return_value={'status':'pending'}):
            pending = action(block,'opening','cny-opening-busy',lambda:block.create_opening_balance({'operation_date':'2026-07-24','cny_amount':'200','rub_value':'2000'}))
        assert pending['acceptance']['durable_saved'] and not pending['acceptance']['processing']['complete']
        assert pending['acceptance']['children'][0]['financial_applied'] is None
        assert not cny.read_status(block)['financial_authority']['current']
        restarted = CnyLedgerBlock(runtime=RegistryUploadDbBackedRuntime(runtime_dir=rt.runtime_dir), timestamp_factory=block.timestamp_factory)
        restarted.replay_ledger(reason='restart-native-worker')
        ready = cny.read_request(rt.runtime_dir,rt.db_path,'cny-opening-busy',request_scope=SCOPE)
        assert ready['acceptance']['processing']['complete'] and cny.read_status(restarted)['financial_authority']['current']
        # Dependent payments use the actual native sequence and sufficient funds.
        for identity, amount, moment in [('pay80','80','2026-07-24T10:00:00Z'),('pay130','130','2026-07-24T10:01:00Z')]:
            saved = action(restarted,'upload','cny-'+identity,lambda identity=identity,amount=amount,moment=moment:(_save_payment(rt,identity,'source',moment,amount) or {'document_id':identity}))
            child = saved['acceptance']['children'][0]
            assert child['financial_applied'] is (identity=='pay80'), saved
        assert rt.load_cny_ledger_replay_state()['balance_cny']=='120'
        assert not saved['acceptance']['processing']['complete']
        before = ledger_business(rt)
        cny.read_request(rt.runtime_dir,rt.db_path,'cny-pay130',request_scope=SCOPE)
        assert ledger_business(rt)==before
        for name, target in [('exclude',''),('restore','target'),('relink','source')]:
            native = (lambda:restarted.delete_document('pay80')) if name=='exclude' else (lambda name=name,target=target:getattr(restarted,name+'_document')('pay80',target_shipment_id=target))
            outcome = action(restarted,name,'cny-pay80-'+name,native,document_id='pay80',target_shipment_id=target)
            assert outcome['acceptance']['durable_saved'],outcome
            if target:assert {'source','target'} <= set(outcome['acceptance']['children'][0]['source_scope']['shipment_ids'])
        assert intents.read_account_request(rt)['status'] != 'delivered'
    with TemporaryDirectory(prefix='operator-cny-upload-') as raw:
        rt, block=setup(raw);block.pdf_text_extractor=_fixture_text_extractor
        fields={'request_id':'cny-native-upload'}
        first=cny.upload(block,b'conversion-fixture',fields=fields,filename='conversion.pdf',content_type='application/pdf',request_scope=SCOPE,actor='alice')
        assert first['acceptance']['durable_saved'],first
        same=cny.upload(block,b'conversion-fixture',fields={**fields,'request_id':'cny-native-upload-alias'},filename='conversion.pdf',content_type='application/pdf',request_scope=SCOPE,actor='alice')
        assert first['results'][0]['document_id']==same['results'][0]['document_id']
        entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
        assert entry.handle_supplier_operator_operation_read(first['acceptance']['operation_id'],request_scope=SCOPE)['domain']==cny.DOMAIN
    with TemporaryDirectory(prefix='operator-cny-cohort-') as raw:
        rt, block = setup(raw)
        for index in range(35):
            identity='cohort-pay-'+str(index)
            moment='2026-07-24T10:'+str(index).zfill(2)+':00Z'
            with patch.object(block,'replay_ledger',return_value={'status':'pending'}):
                admitted=action(block,'upload','cny-'+identity,lambda identity=identity,moment=moment:(_save_payment(rt,identity,'source',moment,'1') or {'document_id':identity}))
            assert admitted['acceptance']['durable_saved'] and not admitted['acceptance']['processing']['complete']
        block.replay_ledger(reason='bounded-cohort-native-worker')
        for index in range(35):
            child=cny.read_request(rt.runtime_dir,rt.db_path,'cny-cohort-pay-'+str(index),request_scope=SCOPE)['acceptance']['children'][0]
            assert child['financial_applied'] is True,child
        assert cny.read_status(block)['financial_authority']['current']
    print('Native CNY source/core-only authority, monotonic revision, same-ID/restart, no false current balance, insufficient payment, exclude/restore/relink and isolated domain: OK')


if __name__ == '__main__': main()
