"""Offline adversarial checks for portal registration; no WB client is used."""
import base64
import hashlib
import json
from io import BytesIO
from openpyxl import Workbook
from copy import deepcopy
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.production_apply_launcher import execute
from packages.application.change_registry import ChangeRegistryRepository, canonical_digest, target_identity
from packages.application.change_registry_external import ExternalRegistryAdapter, decorate_facts, effective_changes, normalize, EVIDENCE, BATCHES
from packages.application.change_registry_baseline_engine import ChangeRegistryBaselineEngine
from apps.change_registry_baseline_engine_smoke import _acquisition, _price, _campaign
import apps.change_registry_baseline_engine_smoke as fixture
fixture.SELLER = "seller-test"
fixture.ACCOUNT = "seller-portal-primary"


def rejected(call):
    try:
        call()
    except (ValueError, RuntimeError, sqlite3.IntegrityError):
        return
    raise AssertionError('unsafe candidate accepted')


def proof_blob(raw):
    return {'artifact_digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'artifact_base64': base64.b64encode(raw).decode()}


def native_proof(row):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(row))
    sheet.append(list(row.values()))
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return proof_blob(buffer.getvalue())


def assert_gap_refused():
    """A missing middle value must not produce a falsely refined timeline."""
    with TemporaryDirectory(prefix='external-registry-gap-') as tmp:
        root = Path(tmp)
        repo = ChangeRegistryRepository(root)
        repo.initialize_schema()
        engine = ChangeRegistryBaselineEngine(runtime_dir=root, seller_id=fixture.SELLER, account_scope=fixture.ACCOUNT)
        for hour, value in [(10, 900), (11, fixture._exact_null()), (14, 700)]:
            engine.ingest(_acquisition(started_at=f'2026-11-03T{hour}:00:00Z', completed_at=f'2026-11-03T{hour}:01:00Z', status='complete', prices=[_price(1, original=1000, discount=0, seller=value)], campaigns=[]))
        fact = repo.list_facts(seller_id=fixture.SELLER, account_scope=fixture.ACCOUNT)['items'][0]
        receipt = {'nm_id': 1, 'advert_id': 0, 'placement': '', 'parameter_field': 'seller_price_minor', 'before_value_integer': 900, 'after_value_integer': 700, 'submitted_from': '2026-11-03T10:30:00Z', 'verified_at': '2026-11-03T10:31:00Z'}
        record = {'fact_id': fact['fact_id'], 'source': 'wb_portal', 'reason': 'Evidence gap regression', 'steps': [{'before_value_integer': 900, 'after_value_integer': 700, 'observed_from': receipt['submitted_from'], 'observed_to': receipt['verified_at'], 'evidence': {'kind': 'portal_submit_readback', 'reference': 'portal/gap-receipt', **proof_blob(json.dumps({'receipts': [receipt]}).encode()), 'receipt': receipt}}]}
        # The attached receipt is valid in isolation. Only the missing observer
        # state makes this candidate unsafe for the existing projection engine.
        assert normalize(record, fact)['steps'][0]['after_value_integer'] == 700
        request = {'runtime_dir': str(root), 'hostname': socket.gethostname(), 'seller_id': fixture.SELLER, 'account_scope': fixture.ACCOUNT, 'actor': 'operator', 'records': [record]}
        before = engine.project_intervals(target=target_identity('price', nm_id=1), parameter_field='seller_price_minor')
        adapter = ExternalRegistryAdapter()
        try:
            adapter.preview(request, 'external-gap-refusal')
        except RuntimeError as exc:
            assert 'пропуск наблюдений' in str(exc), exc
        else:
            raise AssertionError('missing intermediate observation was refined')
        assert adapter.readback(request, 'external-gap-refusal')['state'] == 'not_submitted'
        assert engine.project_intervals(target=target_identity('price', nm_id=1), parameter_field='seller_price_minor') == before
        with repo.store_registry.session('operational', mode='ro', operation='gap-refusal-readback') as conn:
            assert not conn.execute('SELECT 1 FROM sqlite_master WHERE name IN (?,?)', (EVIDENCE, BATCHES)).fetchone(), 'rejected candidate wrote schema'
            assert conn.execute('SELECT COUNT(*) FROM change_registry_facts').fetchone()[0] == 1


def assert_native_installation_semantics(record, fact):
    """Only a native installation from zero can retain observer-proven before."""
    installation = deepcopy(record)
    step = installation['steps'][0]
    row = step['evidence']['row']
    row['Было'] = '0'
    row['Изменение'] = 'Установлена ставка'
    step['evidence'].update(native_proof(row))
    normalized = normalize(installation, fact)['steps'][0]
    assert normalized['before_value_integer'] == fact['before_value_integer'] == 1200
    assert normalized['native_before_value_integer'] == 0
    assert normalized['before_value_source'] == 'canonical_observer_fact'
    assert normalized['evidence']['row']['Было'] == '0', 'native row was rewritten'
    assert normalized['time_precision'] == 'exact_second'
    assert normalized['evidence_note']
    for key, value in [('Изменение', 'Изменена ставка'), ('Было', '1')]:
        bad = deepcopy(installation)
        bad_row = bad['steps'][0]['evidence']['row']
        bad_row[key] = value
        bad['steps'][0]['evidence'].update(native_proof(bad_row))
        rejected(lambda: normalize(bad, fact))
    # An intermediate 1000 value was never observed by the canonical fact.
    # A second native installation from zero cannot claim that provenance.
    chain = deepcopy(record)
    first = chain['steps'][0]
    first['after_value_integer'] = 1000
    first['evidence']['row']['Стало'] = '10'
    first['evidence'].update(native_proof(first['evidence']['row']))
    second = deepcopy(installation['steps'][0])
    second['before_value_integer'] = 1000
    second['observed_from'] = second['observed_to'] = '2026-11-03T13:00:00Z'
    second['evidence']['row']['Дата и время (GMT +3)'] = '2026-11-03 16:00:00'
    second['evidence'].update(native_proof(second['evidence']['row']))
    chain['steps'].append(second)
    rejected(lambda: normalize(chain, fact))


def main():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = ChangeRegistryRepository(root)
        repo.initialize_schema()
        engine = ChangeRegistryBaselineEngine(runtime_dir=root, seller_id='seller-test', account_scope='seller-portal-primary')
        for hour, price, bid in [(10, 900, 1200), (14, 700, 900)]:
            engine.ingest(_acquisition(started_at=f'2026-11-03T{hour}:00:00Z', completed_at=f'2026-11-03T{hour}:01:00Z', status='complete', prices=[_price(1, original=1000, discount=1000, seller=price)], campaigns=[_campaign(10, 1, state='active', model='cpm', bid=bid)]))
        facts = repo.list_facts(seller_id='seller-test', account_scope='seller-portal-primary')['items']
        price = next(f for f in facts if f['parameter_field'] == 'seller_price_minor')
        bid = next(f for f in facts if f['parameter_field'] == 'bid_minor')
        # Helper campaign has combined placement, matching native single bid.
        native = {'ID кампании':10,'ID товара':'1','Что изменилось':'Единая ставка','Кластер':'-', 'Источник':'Личный кабинет','Было':'12','Стало':'9','Дата и время (GMT +3)':'2026-11-03 15:00:00'}
        wb=Workbook();sheet=wb.active;sheet.append(list(native));sheet.append(list(native.values()));buffer=BytesIO();wb.save(buffer);native_blob=proof_blob(buffer.getvalue())
        native_step = {'before_value_integer':1200,'after_value_integer':900,'observed_from':'2026-11-03T12:00:00Z','observed_to':'2026-11-03T12:00:00Z','evidence':{'kind':'wb_native_bid_history','reference':'wb/history10.xlsx',**native_blob,'row':native}}
        def interval(before,after,start,end):
            receipt={'nm_id':1,'advert_id':0,'placement':'','parameter_field':'seller_price_minor','before_value_integer':before,'after_value_integer':after,'submitted_from':start,'verified_at':end}
            return {'before_value_integer':before,'after_value_integer':after,'observed_from':start,'observed_to':end,'evidence':{'kind':'portal_submit_readback','reference':'portal/price-receipt',**proof_blob(json.dumps({'receipts':[receipt]}).encode()),'receipt':receipt}}
        records=[{'fact_id':bid['fact_id'],'source':'wb_portal','reason':'Restore search visibility','steps':[native_step]}, {'fact_id':price['fact_id'],'source':'wb_portal','reason':'Price parity','steps':[interval(900,800,'2026-11-03T11:00:00Z','2026-11-03T11:01:00Z'),interval(800,700,'2026-11-03T13:00:00Z','2026-11-03T13:01:00Z')]}]
        assert_native_installation_semantics(records[0], bid)
        request={'runtime_dir':str(root),'hostname':socket.gethostname(),'seller_id':'seller-test','account_scope':'seller-portal-primary','actor':'operator','records':records}
        adapter=ExternalRegistryAdapter()
        preview=adapter.preview(request,'external-smoke-1')
        assert preview['summary']=={'facts':2,'new_or_refined':2,'duplicates':0,'exact_steps':1,'interval_steps':2}
        with repo.store_registry.session('operational',mode='ro',operation='check-preview') as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(EVIDENCE,)).fetchone(), 'preview wrote schema'
        for mutation in ['seller','native','time','chain','duplicate','unknown','artifact']:
            bad=deepcopy(request)
            if mutation=='artifact':bad['records'][0]['steps'][0]['evidence']['artifact_digest']='sha256:'+'0'*64
            if mutation=='seller':bad['seller_id']='another-seller'
            if mutation=='native':bad['records'][0]['steps'][0]['evidence']['row']['ID кампании']=999
            if mutation=='time':bad['records'][0]['steps'][0]['observed_to']='2026-11-03T15:00:00Z'
            if mutation=='chain':bad['records'][1]['steps'][1]['before_value_integer']=900
            if mutation=='duplicate':bad['records'].append(bad['records'][0])
            if mutation=='unknown':bad['records'][0]['fact_id']='missing-fact'
            rejected(lambda:adapter.preview(bad,'external-bad'))
        with patch('socket.create_connection',side_effect=AssertionError('Network forbidden')):
            receipt=execute(action='apply',adapter_name='change_registry_external_v1',operation_id='external-smoke-1',request=request,expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        assert receipt['state']=='applied'
        original_receipt = adapter.readback(request,'external-smoke-1')
        assert original_receipt['created']==2
        assert adapter.preview(request,'external-smoke-2')['summary']['duplicates']==2
        p2=adapter.preview(request,'external-smoke-2')
        execute(action='apply',adapter_name='change_registry_external_v1',operation_id='external-smoke-2',request=request,expected_prestate=p2['prestate_sha256'],expected_candidate=p2['candidate_sha256'])
        with repo.store_registry.session('operational',mode='ro',operation='check-result') as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM {EVIDENCE}').fetchone()[0]==2
            assert conn.execute('SELECT COUNT(*) FROM change_registry_facts').fetchone()[0]==len(facts)
            changes=effective_changes(decorate_facts(conn,facts))
            assert len(changes)==3 and len({x['change_id'] for x in changes})==3
        result=engine.project_intervals(target=target_identity('price',nm_id=1),parameter_field='seller_price_minor')['items']
        assert len(result)==3, result
        assert result[0]['end_at'].startswith('2026-11-03T11:00:00') and result[1]['start_at'].startswith('2026-11-03T11:01:00'), result
        assert result[1]['end_at'].startswith('2026-11-03T13:00:00') and result[2]['start_at'].startswith('2026-11-03T13:01:00'), result
        result=engine.project_intervals(target=target_identity('bid',nm_id=1,advert_id=10,placement=bid['placement']),parameter_field='bid_minor')['items']
        assert result[0]['end_at']==result[1]['start_at']=='2026-11-03T12:00:00.000000Z', result
        # Revision requires exact prior fingerprint; stale preview cannot write.
        revised=deepcopy(request);revised['records'][0]['reason']='Corrected explanation'
        rejected(lambda:adapter.preview(revised,'external-revise'))
        with repo.store_registry.session('operational',mode='ro',operation='read-prior') as conn:
            prior=conn.execute(f'SELECT payload_digest FROM {EVIDENCE} WHERE fact_id=?',(bid['fact_id'],)).fetchone()[0]
        revised['records'][0]['supersedes']=prior
        pr=adapter.preview(revised,'external-revise')
        adapter.apply(revised,'external-revise',pr)
        rejected(lambda:adapter.apply(request,'external-stale',p2))
        assert adapter.readback(request,'external-smoke-1') == original_receipt, 'old operation receipt changed after refinement'
        conflicting_old = deepcopy(request)
        conflicting_old['records'][0]['reason'] = 'Changed old operation payload'
        rejected(lambda: adapter.readback(conflicting_old, 'external-smoke-1'))
        with repo.store_registry.session('operational',mode='rw',operation='test-immutable') as conn:
            rejected(lambda:conn.execute(f"DELETE FROM {EVIDENCE}"))
        assert_gap_refused()
        print('external registry smoke: PASS (scope, chronology, native proof, installation provenance, chains, duplicates, CAS, gap refusal, historical readback, immutable originals, zero network)')


if __name__=='__main__':
    main()
