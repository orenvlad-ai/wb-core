#!/usr/bin/env python3
"""Synthetic contracts for buyer support read-only vertical slice."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_buyer_support import BuyerSupportRepository, BuyerSupportSync, BuyerSupportValidationError
from packages.adapters.wb_buyer_support import WbBuyerSupportReadAdapter, BuyerSupportApiError, _NoRedirect


FIXTURE_TIME = datetime.now(timezone.utc)


def event(eid='e1', chat='c1', rid='r1', nm=1, text='Question'):
    attachments = {'goodCard': {'rid': rid, 'nmID': nm, 'name': 'Glass'}} if rid else {}
    return {'eventID': eid, 'chatID': chat, 'sender': 'client', 'addTimestamp': int(FIXTURE_TIME.timestamp() * 1000),
            'addTime': '2026-10-04T01:02:03Z', 'clientName': 'Buyer', 'replySign': 'PRIVATE_SIGNATURE',
            'message': {'text': text, 'attachments': attachments}}


def page(events, cursor=101):
    return {'result': {'events': events, 'next': cursor, 'totalEvents': len(events)}, 'errors': None}


def claim(cid='claim1', srid='r1', nm=1):
    return {'id': cid, 'srid': srid, 'nm_id': nm, 'status': 0, 'status_ex': 0,
            'actions': ['rejectcustom'], 'user_comment': '<script>customer</script>',
            'photos': [], 'video_paths': [], 'dt': '2026-10-04T01:02:03', 'dt_update': '2026-10-04T01:03:03'}


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.repo = BuyerSupportRepository(Path(self.temp.name))
    def tearDown(self): self.temp.cleanup()
    def seed(self, events=None): self.repo.save_event_page('A', page(events or [event()]), expected_cursor=None)
    def test_dedup_and_cursor_atomic_rollback(self):
        self.seed()
        self.repo.save_event_page('A', page([event()], 102), expected_cursor=101)
        self.assertEqual(len(self.repo.detail('A', kind='chat', item_id='c1')['messages']), 1)
        with self.assertRaises(BuyerSupportValidationError):
            self.repo.save_event_page('A', page([event('e2'), event(text='conflict')], 103), expected_cursor=102)
        self.assertEqual(self.repo.cursor('A'), 102)
        self.assertEqual(len(self.repo.detail('A', kind='chat', item_id='c1')['messages']), 1)
        with self.assertRaises(BuyerSupportValidationError):
            self.repo.save_event_page('A', page([event('e3'), {'eventID':'bad'}],103), expected_cursor=102)
        self.assertEqual(self.repo.cursor('A'),102)
        with self.assertRaises(BuyerSupportValidationError):
            self.repo.save_event_page('A',page([event('e4')],104),expected_cursor=100)
        self.assertEqual(self.repo.cursor('A'),102)
    def test_private_raw_never_public_and_read_has_no_write(self):
        self.seed()
        before = self.repo.path.read_bytes()
        result = self.repo.detail('A',kind='chat',item_id='c1')
        self.assertNotIn('PRIVATE_SIGNATURE',json.dumps(result))
        self.assertNotIn('raw',result)
        self.assertEqual(self.repo.path.read_bytes(),before)
        with closing(sqlite3.connect(self.repo.path)) as db:
            self.assertNotIn('PRIVATE_SIGNATURE',db.execute('SELECT raw FROM events').fetchone()[0])
        self.assertEqual(self.repo.path.stat().st_mode & 0o777,0o600)
    def test_strict_cabinet_purchase_link_and_ambiguity(self):
        self.seed([event(),event('e2',rid='r2'),event('e3',rid=None)])
        self.repo.save_claim_page('A',{'claims':[claim(),claim('x','missing')],'total':2},archive=False)
        self.repo.save_claim_page('B',{'claims':[claim()],'total':1},archive=False)
        self.assertEqual(self.repo.detail('B',kind='claim',item_id='claim1')['claims'][0]['link_state'],'orphan')
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]['link_state'],'linked')
        messages = self.repo.detail('A',kind='chat',item_id='c1')['messages']
        self.assertEqual(messages[-1]['purchase_link_state'],'ambiguous')
        self.assertIsNone(messages[-1]['candidate_rid'])
        self.repo.save_event_page('A',page([event('e4',chat='c2')],102),expected_cursor=101)
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]['link_state'],'ambiguous')
        self.repo.save_claim_page('A',{'claims':[claim('conflict',nm=999)],'total':1},archive=False)
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='conflict')['claims'][0]['link_state'],'ambiguous')
        with self.assertRaises(KeyError): self.repo.detail('B',kind='chat',item_id='c1')
    def test_recent_period_uses_latest_message_and_detail_keeps_early_seller_history(self):
        def dated(eid, chat, days, sender='client'):
            e=event(eid,chat=chat)
            timestamp=FIXTURE_TIME-timedelta(days=days)
            e.update(addTimestamp=int(timestamp.timestamp()*1000),addTime=timestamp.isoformat(),sender=sender)
            return e
        self.seed([dated('early-manager','recent',150,'seller'),dated('recent-buyer','recent',10),
                   dated('middle','middle',60),dated('old','old',110)])
        self.repo.save_chats('A',{'result':[{'chatID':'undated'}]})
        self.repo.save_claim_page('A',{'claims':[claim('orphan','other')],'total':1},archive=False)
        ninety=self.repo.list_items('A',now=FIXTURE_TIME)
        self.assertEqual(ninety['period'],'90d')
        self.assertEqual({(i['kind'],i['id']) for i in ninety['items']},{('chat','recent'),('chat','middle'),('claim','orphan')})
        thirty=self.repo.list_items('A',period='30d',now=FIXTURE_TIME)
        self.assertEqual({i['id'] for i in thirty['items']},{'recent','orphan'})
        self.assertEqual(self.repo.list_items('A',period='all',now=FIXTURE_TIME)['total'],5)
        messages=self.repo.detail('A',kind='chat',item_id='recent')['messages']
        self.assertEqual([m['id'] for m in messages],['early-manager','recent-buyer'])
        self.assertEqual(messages[0]['sender'],'seller')
        self.assertEqual(ninety['history']['state'],'partial')
        self.assertEqual(ninety['history']['event_count'],4)
        self.assertEqual(ninety['history']['undated_chat_count'],1)
        self.assertEqual(self.repo.list_items('A',period='90d',filter_state='orphan',now=FIXTURE_TIME)['total'],1)
        self.assertEqual(self.repo.list_items('A',period='30d',query='middle',now=FIXTURE_TIME)['total'],0)
        with self.assertRaises(BuyerSupportValidationError):self.repo.list_items('A',period='365d')
    def test_history_not_loaded_partial_error_and_complete_empty(self):
        self.assertEqual(self.repo.list_items('A')['history']['state'],'not_loaded')
        self.seed()
        before=self.repo.list_items('A')['history']
        self.repo.state('A','events','error')
        error=self.repo.list_items('A')['history']
        self.assertEqual(error['state'],'error')
        self.assertEqual(error['last_sync_at'],before['last_sync_at'])
        self.repo.save_event_page('B',page([],102),expected_cursor=None)
        empty=self.repo.list_items('B')['history']
        self.assertEqual(empty['state'],'complete');self.assertEqual(empty['event_count'],0)
        self.assertIsNone(empty['last_sync_at'])
        self.repo.save_event_page('A',page([],102),expected_cursor=101)
        complete=self.repo.list_items('A')['history']
        self.assertEqual(complete['last_sync_at'],before['last_sync_at'])
    def test_orphan_and_missing_purchase_are_separate(self):
        self.repo.save_claim_page('A',{'claims':[claim(),claim('missing',srid=None)],'total':2},archive=False)
        self.assertEqual(self.repo.list_items('A',filter_state='orphan')['total'],1)
        self.assertEqual(self.repo.list_items('A',filter_state='unlinked')['total'],1)
        with closing(sqlite3.connect(self.repo.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM purchases').fetchone()[0],1)
    def test_snapshot_product_metadata_enriches_and_retains_conflicts(self):
        self.repo.save_chats('A',{'result':[{'chatID':'c1','goodCard':{'rid':'r1'}}]})
        self.repo.save_chats('A',{'result':[{'chatID':'c1','goodCard':{'rid':'r1','nmID':1}}]})
        self.repo.save_claim_page('A',{'claims':[claim()],'total':1},archive=False)
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]['link_state'],'linked')
        self.repo.save_chats('A',{'result':[{'chatID':'c1','goodCard':{'rid':'r1','nmID':2}}]})
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]['link_state'],'ambiguous')
        self.assertEqual({p['nm'] for p in self.repo.detail('A',kind='chat',item_id='c1')['purchases']},{None,'1','2'})
        self.repo.save_claim_page('A',{'claims':[claim('no_nm',nm=None)],'total':1},archive=False)
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='no_nm')['claims'][0]['link_state'],'ambiguous')
        other=BuyerSupportRepository(Path(self.temp.name)/'before-claim')
        for nm in (1,2):
            other.save_chats('A',{'result':[{'chatID':'c1','goodCard':{'rid':'r1','nmID':nm}}]})
        self.assertEqual(other.detail('A',kind='chat',item_id='c1')['link_state'],'ambiguous')
    def test_missing_inline_is_candidate_even_after_complete_history(self):
        self.seed([event(),event('e2',rid=None)])
        self.repo.save_event_page('A',page([],102),expected_cursor=101)
        m=self.repo.detail('A',kind='chat',item_id='c1')['messages'][-1]
        self.assertEqual(m['purchase_link_state'],'observed_single_purchase')
        self.assertNotIn('derived_rid',m)
    def test_repeated_claim_scans_do_not_disappear_or_fabricate_deadline(self):
        p={'claims':[claim()],'total':1}
        self.repo.save_claim_page('A',p,archive=False)
        self.repo.save_claim_page('A',p,archive=False)
        self.repo.save_claim_page('A',{'claims':[],'total':0},archive=False)
        c=self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]
        self.assertFalse(c['archive']); self.assertIsNone(c['deadline_at']); self.assertEqual(c['time_zone'],'unknown')
        self.repo.save_claim_page('A',p,archive=True)
        self.assertTrue(self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]['archive'])
    def test_errors_are_not_empty_pages(self):
        for p in ({'error':True,'claims':[],'total':0},{'claims':[]},{'total':0},[] ):
            with self.assertRaises(BuyerSupportValidationError): self.repo.save_claim_page('A',p,archive=False)
        self.assertFalse(self.repo.path.exists())
    def test_partial_scan_preserves_prior_records_and_reports_error(self):
        self.seed()
        adapter=Mock()
        adapter.fetch_chats.return_value={'result':[]}
        adapter.fetch_events.return_value=page([],102)
        adapter.fetch_claims.side_effect=[{'claims':[claim()],'total':201},RuntimeError('private body')]
        with self.assertRaises(RuntimeError): BuyerSupportSync(self.repo,adapter,page_pause_seconds=0).run('A')
        self.assertEqual(self.repo.list_items('A')['sync'][-1]['state'] in ('complete','partial','error'),True)
        state={s['source']:s['state'] for s in self.repo.list_items('A')['sync']}
        self.assertEqual(state['claims_active'],'error')
        self.assertEqual(self.repo.detail('A',kind='claim',item_id='claim1')['claims'][0]['id'],'claim1')
    def test_claim_pagination_active_archive_and_loop_guard(self):
        adapter=Mock()
        adapter.fetch_chats.return_value={'result':[]}
        adapter.fetch_events.side_effect=[page([event()],101),page([],102)]
        adapter.fetch_claims.side_effect=[{'claims':[claim()],'total':2},{'claims':[claim('c2','r2')],'total':2},{'claims':[],'total':0}]
        counts=BuyerSupportSync(self.repo,adapter,page_pause_seconds=0).run('A')['counts']
        self.assertEqual(counts['claims_active'],2)
        self.assertEqual([call.kwargs['offset'] for call in adapter.fetch_claims.call_args_list],[0,1,0])
        adapter.fetch_events.side_effect=None; adapter.fetch_events.return_value=page([],102)
        adapter.fetch_claims.side_effect=[{'claims':[claim()],'total':2},{'claims':[claim()],'total':2}]
        with self.assertRaises(BuyerSupportValidationError): BuyerSupportSync(self.repo,adapter,page_pause_seconds=0).run('A')


class BootstrapTest(unittest.TestCase):
    def test_cli_maintenance_barrier_blocks_before_adapter_and_storage(self):
        from packages.application.business_data_procedure_admission import initialize_admission
        from packages.application.business_data_write_barrier import acquire_barrier
        with TemporaryDirectory() as tmp:
            runtime=Path(tmp)
            initialize_admission(runtime)
            acquire_barrier(runtime,window_id='buyer-support-test',window_kind='maintenance_pause',
                            plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-approval',
                            actor='synthetic-test',reason='blocked bootstrap regression')
            before={p.relative_to(runtime) for p in runtime.rglob('*')}
            marker=runtime/'adapter-was-constructed'
            code="""from pathlib import Path
import apps.wb_buyer_support_sync as cli
import sys
def forbidden_adapter(**kwargs):
    Path(sys.argv[sys.argv.index('--runtime-dir')+1],'adapter-was-constructed').write_text('unexpected')
    raise RuntimeError('network boundary was reached')
cli.WbBuyerSupportReadAdapter=forbidden_adapter
raise SystemExit(cli.main())
"""
            result=subprocess.run([sys.executable,'-c',code,'--runtime-dir',str(runtime),'--cabinet','fixture',
                                   '--token-rate-class','personal-service'],cwd=ROOT,
                                  env={**os.environ,'WB_API_TOKEN':''},capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],'skipped_maintenance')
            self.assertFalse(marker.exists());self.assertFalse((runtime/'buyer-support').exists())
            self.assertEqual({p.relative_to(runtime) for p in runtime.rglob('*')},before)
    def test_hosted_unit_uses_the_existing_account_scope(self):
        unit=(ROOT/'artifacts/registry_upload_http_entrypoint/systemd/wb-core-registry-http.service').read_text()
        self.assertIn('Environment=WB_BUYER_SUPPORT_CABINET_ID=seller-portal-primary',unit)
        self.assertIn('Environment=CHANGE_REGISTRY_ACCOUNT_SCOPE=seller-portal-primary',unit)


class AdapterTest(unittest.TestCase):
    def test_no_write_capability_and_safe_upstream_errors(self):
        adapter=WbBuyerSupportReadAdapter()
        for method in ('post','patch','send','decide','publish'):
            self.assertFalse(hasattr(adapter,method))
        with patch.dict('os.environ',{'WB_API_TOKEN':'PRIVATE_TOKEN'}):
            response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
            response.read.return_value=b'{"result": []}'
            opener=Mock(); opener.open.return_value=response
            with patch('packages.adapters.wb_buyer_support.request.build_opener',return_value=opener):
                adapter.fetch_chats(); adapter.fetch_events(None); adapter.fetch_claims(archive=True,offset=0,limit=200)
                for call in opener.open.call_args_list: self.assertEqual(call.args[0].get_method(),'GET')
                opener.open.side_effect=error.HTTPError('https://host',403,'PRIVATE_TOKEN',{},None)
                with self.assertRaises(BuyerSupportApiError) as caught: adapter.fetch_chats()
                self.assertNotIn('PRIVATE_TOKEN',str(caught.exception))
                self.assertEqual(caught.exception.http_status,403)
            with self.assertRaises(BuyerSupportApiError):
                _NoRedirect().redirect_request(request.Request('https://buyer-chat-api.wildberries.ru',headers={'Authorization':'PRIVATE_TOKEN'}),None,302,'redirect',{},'https://evil.test')


class HttpTest(unittest.TestCase):
    def test_authenticated_local_get_scope_errors_and_absent_mutation_routes(self):
        from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        with TemporaryDirectory() as tmp:
            runtime=Path(tmp)
            app=RegistryUploadHttpEntrypoint(runtime)
            app.buyer_support_repository.save_event_page('A',page([event()]),expected_cursor=None)
            app.buyer_support_repository.save_event_page('B',page([event('OTHER','other')]),expected_cursor=None)
            config=RegistryUploadHttpEntrypointConfig('127.0.0.1',0,'/v1/registry-upload/bundle','/plan','/refresh','/status','/operator',runtime)
            def user(handler, config):
                role=handler.headers.get('X-Test-Role')
                return None if role is None else {'role':'operator','allowed_sections':['feedbacks'] if role=='feedbacks' else ['prices']}
            with patch.dict('os.environ',{'WB_BUYER_SUPPORT_CABINET_ID':'A'}), patch('packages.adapters.registry_upload_http_entrypoint._web_auth_config',return_value={'configured':True,'enabled':True}), patch('packages.adapters.registry_upload_http_entrypoint._authenticated_web_user',side_effect=user):
                server=build_registry_upload_http_server(config,entrypoint=app)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base='http://127.0.0.1:'+str(server.server_address[1])+'/v1/sheet-vitrina-v1/feedbacks/buyer-support'
                def fetch(path, role=None, method='GET'):
                    req=request.Request(base+path,headers={'Accept':'application/json',**({'X-Test-Role':role} if role else {})},method=method,data=b'{}' if method!='GET' else None)
                    try:
                        with request.urlopen(req) as response: return response.status,json.loads(response.read())
                    except error.HTTPError as exc: return exc.code,json.loads(exc.read())
                try:
                    self.assertEqual(fetch('/list')[0],401)
                    self.assertEqual(fetch('/list','prices')[0],403)
                    code,body=fetch('/list','feedbacks')
                    self.assertEqual(code,200); self.assertEqual(body['total'],1)
                    self.assertNotIn('OTHER',json.dumps(body));self.assertNotIn('PRIVATE_SIGNATURE',json.dumps(body))
                    self.assertEqual(fetch('/list?cabinet=B','feedbacks')[0],422)
                    self.assertEqual(fetch('/detail?kind=chat&id=other','feedbacks')[0],404)
                    self.assertEqual(fetch('/detail?kind=chat&id=c1','feedbacks')[0],200)
                    self.assertEqual(fetch('/list?limit=999','feedbacks')[0],422)
                    self.assertEqual(fetch('/list?period=365d','feedbacks')[0],422)
                    self.assertEqual(fetch('/list?period=all&filter=all','feedbacks')[0],200)
                    before=app.buyer_support_repository.path.read_bytes()
                    self.assertNotEqual(fetch('/list','feedbacks','POST')[0],200)
                    self.assertEqual(before,app.buyer_support_repository.path.read_bytes())
                    with patch.object(app.buyer_support_repository,'list_items',side_effect=RuntimeError('SECRET')):
                        code,body=fetch('/list','feedbacks')
                        self.assertEqual(code,503);self.assertNotIn('SECRET',json.dumps(body))
                finally:
                    server.shutdown();thread.join(2);server.server_close()

    def test_public_proxy_lists_only_get_for_new_routes(self):
        manifest=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json').read_text())
        rows=[r for r in manifest['routes'] if '/buyer-support/' in r['path']]
        observation=[r for r in rows if '/pilot/' not in r['path']]
        self.assertEqual(len(observation),2)
        self.assertTrue(all(r['methods']==['GET'] for r in observation))
        pilot=[r for r in rows if '/pilot/' in r['path']]
        self.assertEqual(len(pilot),6)
        self.assertTrue(all(r['methods']==(['GET'] if r['path'].endswith('/operation') else ['POST']) for r in pilot))


class UiTest(unittest.TestCase):
    def test_pure_renderer_empty_xss_and_real_history_labels(self):
        script=ROOT/'packages/adapters/templates/wb_buyer_support.js'
        node='''const assert=require('assert');const r=require(process.argv[1]);
const attack='<img src=x onerror=alert(1)>';
const result=r.renderDetail({name:attack,claims:[{id:attack,comment:attack,archive:false,status:0,status_ex:0,link_state:'unlinked'}],messages:[{sender:'seller',text:attack,media_count:1,time:attack,purchase_link_state:'unlinked'}]});
assert(!result.includes('<img'));assert(result.includes('&lt;img'));assert(result.includes('Продавец · история WB'));assert(result.includes('Решения бота пока не рассчитаны'));
assert(r.renderList({configured:false,items:[]},'').includes('ещё не настроен'));
assert(r.renderList({configured:true,items:[]},'').includes('Обращений по выбранным условиям'));
assert(r.renderList({configured:true,items:[],history:{state:'partial'}},'').includes('частично'));
assert(r.renderList({configured:true,items:[],history:{state:'error'}},'').includes('ошибкой'));
assert(r.renderList({configured:true,items:[],history:{state:'not_loaded'}},'').includes('ещё не загружена'));
'''
        subprocess.run(['node','-e',node,str(script)],check=True)
    def test_template_has_isolated_module_and_acl_tab(self):
        from packages.adapters.registry_upload_http_entrypoint import _web_vitrina_ui_base_template, WEB_AUTH_UNIFIED_TAB_SECTIONS, _required_section_for_path, _user_can_access_path
        _web_vitrina_ui_base_template.cache_clear()
        html=_web_vitrina_ui_base_template()
        self.assertIn('Автоответы: OFF',html)
        self.assertNotIn('/* BUYER_SUPPORT_SCRIPT */',html)
        self.assertEqual(WEB_AUTH_UNIFIED_TAB_SECTIONS['buyer-support'],'feedbacks')
        path='/v1/sheet-vitrina-v1/feedbacks/buyer-support/list'
        self.assertEqual(_required_section_for_path(path),'feedbacks')
        self.assertFalse(_user_can_access_path({'role':'supplier','allowed_sections':[]},path))


class BrowserTest(unittest.TestCase):
    def test_full_page_deep_link_reload_and_local_error(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import urlparse, parse_qs
        from packages.adapters.registry_upload_http_entrypoint import _render_sheet_vitrina_web_vitrina_ui
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is not installed in this test environment')
        with TemporaryDirectory() as tmp:
            repo=BuyerSupportRepository(Path(tmp))
            repo.save_event_page('A',page([event(text='<img src=x onerror=alert(1)>')]),expected_cursor=None)
            html=_render_sheet_vitrina_web_vitrina_ui(read_path='/v1/read',operator_path='/operator',refresh_path='/refresh',job_path='/job',allowed_sections=['feedbacks'],active_tab='buyer-support')
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    url=urlparse(self.path); q=parse_qs(url.query)
                    if url.path.endswith('/buyer-support/list'):
                        payload=repo.list_items('A',query=q.get('q',[''])[0],filter_state=q.get('filter',['all'])[0],period=q.get('period',['90d'])[0])
                        body=json.dumps(payload).encode();mime='application/json'
                    elif url.path.endswith('/buyer-support/detail'):
                        body=json.dumps(repo.detail('A',kind=q['kind'][0],item_id=q['id'][0])).encode();mime='application/json'
                    elif url.path=='/':body=html.encode();mime='text/html'
                    else:body=b'{}';mime='application/json'
                    self.send_response(200);self.send_header('Content-Type',mime);self.end_headers();self.wfile.write(body)
            server=HTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    try: browser=p.chromium.launch(headless=True)
                    except Exception: self.skipTest('Chromium test runtime is unavailable')
                    tab=browser.new_page(viewport={'width':1280,'height':900})
                    errors=[];external=[]
                    tab.on('pageerror',lambda exc:errors.append(str(exc)))
                    def route(r):
                        if urlparse(r.request.url).hostname not in ('127.0.0.1','localhost'):
                            external.append(r.request.url);r.abort()
                        else:r.continue_()
                    tab.route('**/*',route)
                    base='http://127.0.0.1:'+str(server.server_address[1])+'/?tab=buyer-support'
                    tab.goto(base)
                    tab.locator('[data-bs-item]').wait_for()
                    tab.locator('[data-bs-item]').click()
                    tab.locator('[data-bs-detail] .bs-bubble').wait_for()
                    self.assertIn('<img src=x',tab.locator('[data-bs-detail]').inner_text())
                    self.assertEqual(tab.locator('[data-bs-detail] img').count(),0)
                    tab.locator('[data-bs-period]').select_option('all')
                    tab.locator('[data-bs-item]').wait_for()
                    self.assertIn('Сохранённые сообщения',tab.locator('[data-bs-history]').inner_text())
                    tab.reload();tab.locator('[data-bs-item]').wait_for()
                    self.assertEqual(errors,[])
                    tab.route('**/buyer-support/list?*',lambda r:r.fulfill(status=503,content_type='application/json',body='{"error":"read_failed"}'))
                    tab.locator('[data-bs-refresh]').click()
                    tab.locator('[data-bs-list] .bs-error').wait_for()
                    self.assertIn('Не удалось прочитать историю',tab.locator('[data-bs-list]').inner_text())
                    self.assertEqual(tab.locator('[data-bs-item]').count(),0)
                    self.assertEqual(errors,[])
                    browser.close()
            finally:
                server.shutdown();thread.join(2);server.server_close()


if __name__ == '__main__': unittest.main()
