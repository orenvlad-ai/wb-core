"""Actual native source/partial batch/CNY core authority with deferred cost."""
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
from unittest.mock import patch
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_processing_smoke import seed,NOW,DAY
from apps.supplier_preparation_intents_smoke import HEADER,LINES
from apps.cny_ledger_smoke import _save_payment
from packages.application import operator_supplier_financial as financial,operator_supplier_shipments as source,cny_preparation_intents as cny
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.registry_upload_db_backed_runtime import _connect


def setup(raw):
    rt=seed(raw)
    for owner in ('source','target'):
        rt.save_supplier_shipment(header={**HEADER,'shipment_id':owner,'created_at':NOW,'updated_at':NOW,'invoice_date':DAY,'shipment_date':DAY,'invoice_no':owner,'invoice_amount_total':100,'product_qty_total':10,'product_amount_total':100},lines=[{**LINES[0],'line_id':owner+'-line','internal_nm_id':1}])
    ledger=CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW)
    ledger.create_opening_balance({'operation_date':DAY,'cny_amount':100,'rub_value':1000})
    return rt,ledger


def execute(rt,identity,manifest,write,after=None,owners=('source',),action='confirm_upload'):
    return financial.execute(rt,action=action,payload={'request_id':identity},shipment_id='source',actor='alice',request_scope='alice-key',manifest=manifest,validate=lambda:list(owners),write_child=write,after_source=after)


def document(rt,child):
    identity=child['subject_id']
    return rt.save_supplier_financial_document(document=dict(document_id=identity,supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=DAY,parse_status='confirmed',total_amount_rub=120,file_sha256='a'*64),expense_lines=[dict(line_id=identity+'-expense',amount=120,amount_rub=120,currency='RUB',category='domestic_transport',status='confirmed')])


def source_and_partial():
    with TemporaryDirectory(prefix='supplier-financial-batch-') as raw:
        rt,_=setup(raw)
        manifest=[{'child_key':'one','kind':'financial','subject_id':'one'},{'child_key':'two','kind':'financial','subject_id':'two'}]
        writes=[]
        def first(child):
            writes.append(child['child_key'])
            if child['child_key']=='two':raise RuntimeError('process died before second source')
            return document(rt,child)
        result=execute(rt,'financial-native-partial',manifest,first)
        assert result['settled'] and result['acceptance']['partial']
        assert [r['status'] for r in result['results']]==['accepted','not_saved']
        assert result['results'][0]['acceptance']['durable_saved']
        assert not result['results'][0]['acceptance']['processing']['complete']
        again=execute(rt,'financial-native-partial',manifest,lambda _:(_ for _ in ()).throw(AssertionError('resubmitted source')))
        assert again['acceptance']['operation_id']==result['acceptance']['operation_id']
        before=rt.db_path.read_bytes()
        with patch.object(financial,'ensure_schema',side_effect=AssertionError('GET bootstrap')):
            read=financial.read_request(rt.runtime_dir,rt.db_path,'financial-native-partial',request_scope='alice-key')
            assert read['results'][0]['operation_id']==result['results'][0]['operation_id']
            assert financial.read_request(rt.runtime_dir,rt.db_path,'financial-native-partial',request_scope='foreign')['status']=='unknown'
        assert rt.db_path.read_bytes()==before
        try:execute(rt,'financial-native-partial',[{'child_key':'other','kind':'financial','subject_id':'other'}],lambda _:(_ for _ in ()).throw(AssertionError('foreign child write')))
        except ValueError as exc:assert 'another source action' in str(exc)
        else:raise AssertionError('same body/identity can address a different native document')
        assert len(rt.list_supplier_financial_expense_lines('source'))==1
        with closing(source.readonly(rt.db_path)) as conn:
            retained=conn.execute(f'SELECT * FROM {financial.CHILDREN}').fetchone()
            assert retained['source_digest']==source.digest(__import__('json').loads(retained['source_json']))
            assert conn.execute(f'SELECT count(*) FROM {financial.SCOPES}').fetchone()[0]==1
        # Corrected remaining file is a distinct action; saved first source is not retried.
        corrected=execute(rt,'financial-native-corrected',[manifest[1]],lambda child:document(rt,child))
        assert corrected['results'][0]['status']=='accepted',corrected
        assert len(rt.list_supplier_financial_expense_lines('source'))==2
    print('native atomic source/receipt/intent; partial exact children; RO/restart same-ID no resubmit; corrected distinct identity: OK')


def ledger_business(rt):
    rows=[{k:r[k] for k in ('operation_id','operation_type','source_document_id','source_order_id','cny_delta','rub_value_delta','effective_rate_before','balance_cny_after','balance_rub_value_after','average_rate_after','status','error_reason')} for r in rt.list_cny_ledger_operations()]
    for row in rows:
        if row['operation_type']=='opening_balance':
            row['operation_id']=row['source_document_id']='<fixture-opening>'
    return rows


def capital_business(rt):
    with closing(source.readonly(rt.db_path)) as conn:
        result=[]
        for row in conn.execute('SELECT * FROM sheet_vitrina_v1_own_capital_payment_layers ORDER BY payment_id'):
            result.append({k:v for k,v in dict(row).items() if k not in {'created_at','updated_at','fingerprint'}})
        return result


def core_and_consumer():
    with TemporaryDirectory(prefix='supplier-financial-core-') as raw,TemporaryDirectory(prefix='supplier-financial-control-') as control_raw:
        rt,ledger=setup(raw);control,native=setup(control_raw)
        def write(_):
            _save_payment(rt,'partial-payment','source',NOW,'20')
            return {'document_id':'partial-payment'}
        def core(child,_):
            assert child['core_needed']
            value=ledger.replay_ledger(reason='attachment-native-core')
            assert value['readback_confirmed'] and value['cost_preparation_pending'] and value['operator_core_retained'],value
        with patch.object(OwnProductCapitalBlock,'recalculate',side_effect=AssertionError('HTTP cost calculation')),patch.object(ledger,'_sync_own_product_capital_payments',side_effect=AssertionError('HTTP capital sync')),patch.object(ledger,'_reconcile_changed_capital_operations',side_effect=AssertionError('HTTP capital removal')):
            result=execute(rt,'financial-partial-payment',[{'child_key':'payment','kind':'cny','subject_id':'partial-payment'}],write,core)
        assert result['results'][0]['acceptance']['financial_applied']
        assert 'balance_cny' not in result['results'][0]['acceptance']['financial_receipt']
        assert rt.load_cny_ledger_replay_state()['balance_cny']=='80'
        assert rt.load_cny_ledger_replay_state()['balance_rub_value']=='800'
        request=cny.read_account_request(rt)
        assert request['status']!='delivered' and not request['prepared_at']
        _save_payment(control,'partial-payment','source',NOW,'20');native.replay_ledger(reason='control')
        assert ledger_business(rt)==ledger_business(control),(ledger_business(rt),ledger_business(control))
        before=ledger_business(rt)
        with patch.object(ledger,'replay_ledger',side_effect=AssertionError('GET repeated core')):
            financial.read_request(rt.runtime_dir,rt.db_path,'financial-partial-payment',request_scope='alice-key')
            execute(rt,'financial-partial-payment',[{'child_key':'payment','kind':'cny','subject_id':'partial-payment'}],lambda _:(_ for _ in ()).throw(AssertionError('same-ID payment retry')),core)
        assert ledger_business(rt)==before
        completed=cny.drain_cny_preparation_intents(rt,block=ledger)
        assert cny.read_account_request(rt)['status']=='delivered',completed
        assert ledger_business(rt)==ledger_business(control),(ledger_business(rt),ledger_business(control))
        assert capital_business(rt)==capital_business(control)
        # A native relink changes source/owner and ledger source, not amount.
        with patch.object(OwnProductCapitalBlock,'recalculate',side_effect=AssertionError('HTTP relink cost calculation')),patch.object(ledger,'_sync_own_product_capital_payments',side_effect=AssertionError('HTTP relink capital sync')),patch.object(ledger,'_reconcile_changed_capital_operations',side_effect=AssertionError('HTTP relink capital removal')):
            moved=execute(rt,'financial-payment-relink',[{'child_key':'move','kind':'cny','subject_id':'partial-payment'}],lambda _:ledger.relink_document('partial-payment',target_shipment_id='target'),owners=('source','target'))
        assert moved['results'][0]['acceptance']['financial_applied']
        assert moved['results'][0]['acceptance']['source_scope']['shipment_ids']==['source','target']
        assert rt.load_cny_ledger_replay_state()['balance_cny']=='80'
        assert cny.read_account_request(rt)['status']!='delivered'
        native.relink_document('partial-payment',target_shipment_id='target')
        assert ledger_business(rt)==ledger_business(control),(ledger_business(rt),ledger_business(control))
        cny.drain_cny_preparation_intents(rt,block=ledger)
        assert capital_business(rt)==capital_business(control)
    print('actual native partial payment/relink ledger and balance unchanged; HTTP no derived capital; durable pending intent; worker same final capital; GET/core no duplicate: OK')

def native_fences_and_restart():
    with TemporaryDirectory(prefix='supplier-core-restart-') as raw:
        rt,ledger=setup(raw)
        def changed():
            with _connect(rt.db_path) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS fixture_core_noise(value INTEGER)")
                conn.execute("INSERT INTO fixture_core_noise VALUES(1)");conn.commit()
        # A real different connection commits AFTER numerical proof, before
        # source receipt CAS. The live observer rejects the mixed proof.
        with patch.object(financial,'_after_core_proof',side_effect=changed):
            value=execute(rt,'financial-core-fenced',[{'child_key':'pay','kind':'cny','subject_id':'pay'}],lambda _:(_save_payment(rt,'pay','source',NOW,'20') or {'document_id':'pay'}),lambda c,r:ledger.replay_ledger(reason='fenced'))
        assert value['results'][0]['acceptance']['financial_applied'] is None
        assert not value['results'][0]['acceptance']['processing']['complete']
        assert cny.read_account_request(rt)['status']!='delivered'
        with patch.object(financial,'_during_core_proof',side_effect=changed):
            during=execute(rt,'financial-core-during-ro',[{'child_key':'during','kind':'cny','subject_id':'during'}],lambda _:(_save_payment(rt,'during','source',NOW,'1') or {'document_id':'during'}),lambda c,r:ledger.replay_ledger(reason='during-ro'))
        assert during['results'][0]['acceptance']['financial_applied'] is None
        # Native consumer after restart owns the actual core/cost readback.
        cny.drain_cny_preparation_intents(rt,block=CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW))
        read=financial.read_request(rt.runtime_dir,rt.db_path,'financial-core-fenced',request_scope='alice-key')
        assert read['results'][0]['acceptance']['financial_applied']
        # A foreign account mutation between batch children cannot enter the
        # next short source writer under this action's original operands.
        manifest=[{'child_key':'a','kind':'cny','subject_id':'a'},{'child_key':'b','kind':'cny','subject_id':'b'}]
        def write(child):
            _save_payment(rt,child['subject_id'],'source',NOW,'5')
            return {'document_id':child['subject_id']}
        def foreign(child,_):
            if child['child_key']=='a':_save_payment(rt,'foreign','target',NOW,'1')
        partial=execute(rt,'financial-account-between-children',manifest,write,foreign,owners=('source','target'))
        assert [r['status'] for r in partial['results']]==['accepted','rejected'],partial
        assert rt.load_cny_document('b') is None
    print('Actual inter-connection commit proof→CAS rejected; native restart/core proof recovery; foreign CNY account source between children guarded: OK')


def partial_batch_truth():
    manifest=[{'child_key':'one','kind':'financial','subject_id':'one'},{'child_key':'two','kind':'financial','subject_id':'two'}]
    for mode in ('interrupted_second','rejected_second','validation_rejected_all','complete_all'):
        with TemporaryDirectory(prefix='financial-batch-truth-') as raw:
            rt,_=setup(raw);document(rt,manifest[0])
            if mode=='complete_all':document(rt,manifest[1])
            writes=[]
            def validate():
                if mode=='validation_rejected_all':raise ValueError('invalid batch before source')
                return ['source']
            def write(child):
                writes.append(child['child_key'])
                if child['child_key']=='two' and mode!='complete_all':
                    if mode=='interrupted_second':raise RuntimeError('process loss before second source')
                    raise ValueError('second document refusal')
                financial.adopt_existing(rt,kind='financial',subject_id=child['subject_id'],owners=child['expected_owners'])
                return {'document_id':child['subject_id']}
            identity='financial-truth-'+mode
            kwargs=dict(action='confirm_upload',payload={'request_id':identity},shipment_id='source',actor='alice',request_scope='alice-key',manifest=manifest)
            result=financial.execute(rt,**kwargs,validate=validate,write_child=write)
            assert result['settled'],result
            receipt=result['acceptance']
            if mode=='validation_rejected_all':
                assert result['status']=='rejected' and receipt is None and writes==[],result
                assert [r['status'] for r in result['results']]==['rejected','rejected'],result
            elif mode=='complete_all':
                assert receipt['durable_saved'] and not receipt['partial'],result
                assert receipt['state']=='completed' and receipt['processing']['complete'],result
                assert [r['status'] for r in result['results']]==['accepted','accepted'],result
                assert all(r['processing']['kind']=='source_only' and r['processing']['complete'] for r in receipt['children']),result
            else:
                assert [r['status'] for r in result['results']]==['accepted','not_saved' if mode=='interrupted_second' else 'rejected'],result
                assert receipt['durable_saved'] and receipt['partial'] and len(receipt['children'])==1,result
                assert receipt['state']=='needs_attention' and not receipt['processing']['complete'],result
                assert 'часть' in receipt['reason_ru'],result
                child=receipt['children'][0]
                assert child['durable_saved'] and child['state']=='completed' and child['processing']['kind']=='source_only' and child['processing']['complete'],result
                assert rt.load_supplier_financial_document(supplier_order_id='source',document_id='two') is None
            before=rt.db_path.read_bytes()
            def never(*_):raise AssertionError('same ID resumed validation/source save')
            assert financial.execute(rt,**kwargs,validate=never,write_child=never)==result
            assert financial.read_request(rt.runtime_dir,rt.db_path,identity,request_scope='alice-key')==result
            assert rt.db_path.read_bytes()==before
    # A later source revision may retire the saved child's cost obligation.
    # Its retained partial parent must still describe the unfinished manifest.
    with TemporaryDirectory(prefix='financial-partial-retired-') as raw:
        rt,_=setup(raw)
        def write(child):
            if child['child_key']=='two':raise RuntimeError('before second source')
            return document(rt,child)
        result=execute(rt,'financial-truth-retired',manifest,write)
        assert result['acceptance']['state']=='needs_attention'
        def replace_source(child):
            return rt.save_supplier_financial_document(document={**rt.load_supplier_financial_document(supplier_order_id='source',document_id='one'),'total_amount_rub':121},expense_lines=[{**r,'amount':121,'amount_rub':121} for r in rt.list_supplier_financial_expense_lines('source')])
        execute(rt,'financial-truth-new-revision',[manifest[0]],replace_source)
        read=financial.read_request(rt.runtime_dir,rt.db_path,'financial-truth-retired',request_scope='alice-key')
        assert read['acceptance']['children'][0]['processing']['terminal'],read
        assert read['acceptance']['children'][0]['state']=='delayed',read
        assert read['acceptance']['state']=='needs_attention' and not read['acceptance']['processing']['complete'],read
        assert read['acceptance']['partial'] and 'часть' in read['acceptance']['reason_ru'],read
        assert not read['acceptance']['processing'].get('terminal'),read
    print('Native mixed completed-child/unsaved-or-rejected batch stays partial attention; saved proofs retained; all refusal/full completion unchanged; exact same-ID no resubmit; superseded subset cannot hide partial: OK')


if __name__=='__main__':
    source_and_partial();partial_batch_truth();core_and_consumer();native_fences_and_restart()
