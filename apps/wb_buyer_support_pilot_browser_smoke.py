#!/usr/bin/env python3
"""Usable loopback-only mocked pilot demo and browser regression checks."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

class Demo:
    def __init__(self):
        self.draft=None;self.operations=[];self.receipts={};self.posts=[];self.version='v1';self.allowlisted=True;self.return_mode=False;self.enabled=True;self.provider_configured=True;self.source_fresh=False;self.claim_confirmed=False;self.generation_unknown=False;self.refresh_failed=False
    def detail(self):
        messages=[{'sender':'client','text':'Здравствуйте! Заказал стекло не для своей модели телефона. Как оформить возврат?','time':'2026-10-06T10:00:00Z','purchase_link_state':'inline'}]
        claims=[]
        if self.return_mode:
            messages += [{'sender':'seller','text':'Подскажите, стекло уже было установлено?','time':'2026-10-06T10:05:00Z','purchase_link_state':'inline'},
                         {'sender':'client','text':'Нет, стекло не устанавливал. Ошибся при выборе модели; товар и упаковка целые. Заявку на возврат уже оформил.','time':'2026-10-06T10:10:00Z','purchase_link_state':'inline'},
                         {'sender':'seller','text':'Проверили: заявка относится к этой покупке.','time':'2026-10-06T10:15:00Z','purchase_link_state':'inline'}]
            claims=[{'id':'fixture-claim','title':'Возврат стекла для другой модели','comment':'Ошибся при выборе модели. Стекло не устанавливал, товар и упаковка целые.','archive':False,'status':1 if self.claim_confirmed else 0,'status_ex':1 if self.claim_confirmed else 0,'link_state':'linked','seen_at':'2026-10-06T10:15:00Z','created_at':'2026-10-06T10:10:00'}]
        return {'id':'owner-test-chat','kind':'chat','name':'Тестовое обращение владельца','claims':claims,'purchases':[{'rid':'fixture-purchase'}],
                'messages':messages,'context_version':self.version,
                'pilot':{'enabled':self.enabled,'allowlisted':self.allowlisted,'provider_configured':self.provider_configured,'wb_configured':True,'automatic_actions':False,'source_fresh':self.source_fresh,'source_refreshed_at':'2026-10-06T10:15:00Z' if self.source_fresh else None,'blocked_codes':[] if self.source_fresh else ['source_refresh_required']},
                'workflow':{'state':'manual','latest_draft':self.draft,'operations':copy.deepcopy(self.operations),'internal_events':[]}}
    def post(self,action,body):
        self.posts.append((action,body));time.sleep(.15)
        if action=='refresh':
            if self.refresh_failed:result={'kind':'refresh','state':'failed','request_id':body['request_id'],'write_attempted':False,'error':'source_sync_busy'}
            else:self.source_fresh=True;result=self.detail()
        elif action=='propose':
            self.draft={'draft_id':'draft-'+str(len(self.posts)),'chat_id':'owner-test-chat','kind':'chat','item_id':'owner-test-chat','status':'ready','text':'Здравствуйте! Подскажите, стекло уже было установлено? Это поможет проверить условия возврата.','context_version':self.version,'policy_hash':'fixture-policy','model_profile':'fixture'}
            if self.return_mode:
                self.draft['text']='Спасибо за уточнение. Проверяем вашу заявку на возврат стекла для другой модели.'
                self.draft['return_proposal']={'claim_id':'fixture-claim','action':'approve2','claim_version':'cv1','basis_type':'text_sufficient','basis':'Ошибка выбора модели. Покупатель подтвердил, что стекло не устанавливал; возврат со сдачей товара.'}
            result=copy.deepcopy(self.draft)
            if self.generation_unknown:
                self.draft=None;result={'kind':'draft','request_id':body['request_id'],'status':'generation_unknown','error':'provider_unavailable'}
        elif action=='send':
            result={'operation_id':'operation-send','request_id':body['request_id'],'kind':'send','state':'unknown','write_attempted':True,'can_reconcile':True};self.operations.append(result);self.draft['status']='sent'
        elif action=='claim':
            result={'operation_id':'operation-claim','request_id':body['request_id'],'kind':'claim_decision','state':'pending_readback','write_attempted':True,'can_reconcile':True};self.operations.append(result)
            self.draft['status']='consumed'
        else:
            result=self.operations[-1];result['state']='confirmed'
            if result['kind']=='claim_decision':
                self.claim_confirmed=True
                self.draft={**self.draft,'status':'ready','draft_id':'after-return','return_proposal':None,'text':'Возврат со сдачей товара одобрен. Зачисление денег проверяйте в личном кабинете WB.'}
        self.receipts[body['request_id']]=result
        return result

@contextmanager
def mock_server():
    from packages.adapters.registry_upload_http_entrypoint import _render_sheet_vitrina_web_vitrina_ui
    demo=Demo()
    html=_render_sheet_vitrina_web_vitrina_ui(read_path='/read',operator_path='/operator',refresh_path='/refresh',job_path='/job',allowed_sections=['feedbacks'],active_tab='buyer-support')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,data,status=200,mime='application/json'):
            body=data.encode() if isinstance(data,str) else json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',mime);self.end_headers();self.wfile.write(body)
        def do_GET(self):
            url=urlparse(self.path);q=parse_qs(url.query)
            if url.path=='/':self.respond(html,mime='text/html');return
            if url.path.endswith('/buyer-support/list'):
                self.respond({'configured':True,'total':1,'items':[{'id':'owner-test-chat','kind':'chat','name':'Тестовое обращение владельца','preview':'Стекло для другой модели','link_state':'linked','time':'2026-10-06T10:00:00Z'}],'sync':[],'history':{'state':'complete','event_count':1,'oldest_message_at':'2026-10-06T10:00:00Z','newest_message_at':'2026-10-06T10:00:00Z'}})
            elif url.path.endswith('/buyer-support/detail'):self.respond(demo.detail())
            elif url.path.endswith('/pilot/operation'):
                result=demo.receipts.get(q.get('request_id',[''])[0]);self.respond(result or {'error':'receipt_not_found'},200 if result else 404)
            else:self.respond({})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            result=demo.post(self.path.rsplit('/',1)[-1],body);self.respond(result,503 if result.get('state')=='failed' else 200)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield demo,'http://127.0.0.1:'+str(server.server_address[1])
    finally:server.shutdown();thread.join(2);server.server_close()

def browser_check(screenshots=None):
    from playwright.sync_api import sync_playwright
    with mock_server() as (demo,base),sync_playwright() as p:
        browser=p.chromium.launch(headless=True);tab=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
        tab.on('pageerror',lambda e:errors.append(str(e)))
        tab.route('**/*',lambda r:r.continue_() if urlparse(r.request.url).hostname in ('127.0.0.1','localhost') else r.abort())
        tab.goto(base+'/?tab=buyer-support');tab.locator('[data-bs-item]').click()
        propose=tab.locator('[data-bs-action="propose"]');propose.wait_for()
        assert propose.is_disabled()
        tab.locator('[data-bs-action="refresh"]').click();tab.wait_for_function("document.querySelector('[data-bs-action=propose]') && !document.querySelector('[data-bs-action=propose]').disabled")
        propose.click();tab.locator('[data-bs-action="send"]').wait_for()
        assert [a for a,b in demo.posts]==['refresh','propose']
        if screenshots:
            screenshots.mkdir(parents=True,exist_ok=True);tab.screenshot(path=str(screenshots/'01-answer-preview.png'),full_page=True)
        tab.once('dialog',lambda d:d.dismiss());tab.locator('[data-bs-action="send"]').click();assert [a for a,b in demo.posts].count('send')==0
        # Lost response after actual acceptance: must resolve by GET, never resubmit POST.
        def lose_response(route):route.fetch();route.abort()
        tab.route('**/pilot/send',lose_response)
        tab.once('dialog',lambda d:d.accept())
        tab.locator('[data-bs-action="send"]').evaluate('(b)=>{b.click();b.click();}')
        tab.locator('[data-bs-check]').wait_for();assert [a for a,b in demo.posts].count('send')==1
        assert tab.locator('[data-bs-action="propose"]').is_disabled()
        tab.reload();tab.locator('[data-bs-item]').click();tab.locator('[data-bs-check]').wait_for()
        tab.locator('[data-bs-check]').click();tab.wait_for_function('!document.querySelector("[data-bs-check]")')
        assert [a for a,b in demo.posts].count('send')==1
        demo.enabled=False;demo.provider_configured=False;tab.locator('[data-bs-refresh]').click();tab.get_by_text('Режим наблюдения. Ручной пилот отключён.',exact=True).wait_for();tab.locator('[data-bs-action="reconcile"]').wait_for()
        assert tab.locator('[data-bs-action="propose"]').count()==0
        tab.locator('[data-bs-action="reconcile"]').click();tab.wait_for_function('!document.querySelector("[data-bs-action=reconcile]")')
        demo.enabled=True;demo.provider_configured=True;tab.locator('[data-bs-refresh]').click();tab.locator('[data-bs-action="propose"]').wait_for()
        # New buyer context invalidates draft before any further action.
        tab.locator('[data-bs-action="propose"]').click();tab.locator('[data-bs-action="send"]').wait_for()
        demo.version='v2';demo.source_fresh=False;demo.return_mode=True;tab.locator('[data-bs-refresh]').click();tab.get_by_text('Черновик устарел',exact=False).wait_for();assert tab.locator('[data-bs-action="send"]').count()==0
        tab.locator('[data-bs-action="propose"]').click();tab.locator('[data-bs-review-return]').wait_for()
        assert tab.locator('[data-bs-action="send"]').count()==1
        tab.locator('[data-bs-review-return]').click()
        if screenshots:tab.screenshot(path=str(screenshots/'02-return-separate-confirmation.png'),full_page=False)
        assert tab.locator('[data-bs-action="claim"]').is_disabled()
        tab.locator('[data-bs-text-basis]').check()
        tab.locator('[data-bs-cancel-return]').click();assert [a for a,b in demo.posts].count('claim')==0
        tab.locator('[data-bs-review-return]').click();assert not tab.locator('[data-bs-text-basis]').is_checked()
        assert 'Со сдачей товара' in tab.locator('[data-bs-return-confirm]').inner_text()
        tab.locator('[data-bs-text-basis]').check();tab.locator('[data-bs-action="claim"]').click()
        tab.locator('[data-bs-action="reconcile"]').wait_for();assert [a for a,b in demo.posts].count('claim')==1
        assert tab.locator('[data-bs-action="send"]').count()==0
        tab.locator('[data-bs-action="reconcile"]').click();tab.locator('[data-bs-action="send"]').wait_for()
        assert [a for a,b in demo.posts].count('send')==1
        if screenshots:tab.screenshot(path=str(screenshots/'03-confirmed-return-answer.png'),full_page=True)
        # A known ambiguous generation blocks repayment for this context, not safe refresh.
        demo.version='v3';demo.generation_unknown=True;tab.locator('[data-bs-refresh]').click();tab.get_by_text('Черновик устарел',exact=False).wait_for()
        tab.route('**/pilot/propose',lose_response)
        tab.locator('[data-bs-action="propose"]').click();tab.locator('[data-bs-check]').wait_for()
        tab.locator('[data-bs-check]').click();tab.wait_for_function('!document.querySelector("[data-bs-check]")')
        assert tab.locator('[data-bs-action="propose"]').is_disabled()
        assert not tab.locator('[data-bs-action="refresh"]').is_disabled()
        before=len(demo.posts);tab.locator('[data-bs-action="propose"]').evaluate('(b)=>b.click()');assert len(demo.posts)==before
        tab.reload();tab.locator('[data-bs-item]').click();tab.locator('[data-bs-action="propose"]').wait_for();assert tab.locator('[data-bs-action="propose"]').is_disabled()
        demo.version='v4';demo.generation_unknown=False;tab.locator('[data-bs-action="refresh"]').click();tab.wait_for_function("document.querySelector('[data-bs-action=propose]') && !document.querySelector('[data-bs-action=propose]').disabled")
        demo.refresh_failed=True;tab.locator('[data-bs-action="refresh"]').click();tab.locator('[data-bs-check]').wait_for()
        tab.locator('[data-bs-check]').click();tab.wait_for_function('!document.querySelector("[data-bs-check]")')
        assert not tab.locator('[data-bs-action="refresh"]').is_disabled()
        demo.refresh_failed=False
        def reject_before_attempt(route):
            body=route.request.post_data_json
            route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'provider_not_configured','code':'provider_not_configured','not_accepted':True,'write_attempted':False,'request_id':body['request_id']}))
        tab.route('**/pilot/propose',reject_before_attempt)
        tab.locator('[data-bs-action="propose"]').click();tab.get_by_text('Действие не принято. Проверьте обновлённое обращение.',exact=True).wait_for()
        assert tab.locator('[data-bs-check]').count()==0
        assert not tab.locator('[data-bs-action="refresh"]').is_disabled()
        demo.allowlisted=False;tab.locator('[data-bs-refresh]').click();tab.get_by_text('Это обращение доступно только',exact=False).wait_for();assert tab.locator('[data-bs-action]').count()==0
        assert errors==[],errors
        browser.close()
        print(json.dumps({'status':'passed','checks':['visible_WB_refresh_before_generation','readback_when_OFF','preview','explicit_confirmation','double_click','lost_response_GET_only','reload_recovery','stale_context','separate_return','explicit_text_basis','generation_unknown_same_context_lock_safe_refresh','failed_read_only_refresh_recovery','pre_attempt_gate503_not_accepted','allowlist'],'screenshots':str(screenshots) if screenshots else None},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
    if args.serve:
        with mock_server() as (demo,url):
            print(url+'/?tab=buyer-support',flush=True)
            try:threading.Event().wait()
            except KeyboardInterrupt:pass
    else:browser_check(args.screenshots)
