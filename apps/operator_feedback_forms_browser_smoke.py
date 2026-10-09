#!/usr/bin/env python3
"""Actual form functions -> real native HTTP/source -> fake WB; no external work."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json,socket,sys,threading
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from apps.wb_autoanswers_publication_test import PublicationTest
from apps.wb_autoanswers_http_ui_test import FakeAutoanswersLifecycle
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.application import sheet_vitrina_v1_feedbacks_complaints as complaint
from packages.adapters import registry_upload_http_entrypoint as web

@contextmanager
def native_server():
    f=PublicationTest();f.setUp()
    try:
        job=f.manual_reviewed();f.env['WB_CORE_WEB_AUTH_USERNAME']='local_operator'
        runtime=Path(f.temp.name)
        bridge=SimpleNamespace(guard_final=lambda **kw:dict(reply=kw['reply'],passed=True,errors=[]))
        app=RegistryUploadHttpEntrypoint(runtime,autoanswers_repository=f.repo,autoanswers_node_bridge=bridge,
            change_registry_read_surface=ChangeRegistryReadSurface(runtime,seller_id='fixture'))
        app.autoanswers_lifecycle=FakeAutoanswersLifecycle(f.repo)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        cfg=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path=web.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',
            sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=runtime)
        server=web.build_registry_upload_http_server(cfg,entrypoint=app)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:yield 'http://127.0.0.1:'+str(port),app,f,job
        finally:server.shutdown();server.server_close();t.join(5)
    finally:f.tearDown()

def main():
    with native_server() as (base,app,f,job),sync_playwright() as pw:
        browser=pw.chromium.launch()
        try:
            page=browser.new_page();errors=[];posts=[];reads=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            # Prevent unrelated dashboard fetches from using provider adapters.
            def all_routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,web.DEFAULT_SHEET_FEEDBACKS_AUTOANSWERS_EDIT_PATH,
                    web.DEFAULT_SHEET_FEEDBACKS_AUTOANSWERS_APPROVE_PATH,web.DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH,
                    web.DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH,'/v1/sheet-vitrina-v1/operations/feedback'} or path.endswith('.js') or path.endswith('.css'):
                    if '/operations/feedback' in path:reads.append(route.request.url)
                    route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            page.route(base+'/**',all_routes)
            def lost(route):
                posts.append((route.request.url,route.request.post_data_json))
                response=route.fetch();
                if not 200<=response.status<300:
                    route.fulfill(response=response);raise AssertionError(response.text())
                route.abort('failed')
            page.route('**/autoanswers/manual/edit',lost)
            page.route('**/autoanswers/review/approve',lost)
            page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
            page.evaluate('detail=>{state.feedbacks.server.detail=detail;state.feedbacks.server.settingsRevision="fixture";}',f.repo.get_feedback(job['feedback_id']))
            body=dict(processing_key=job['processing_key'],reply='Спасибо за отзыв о товаре.')
            result=page.evaluate('async body=>postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,body)',body)
            assert result['status']=='native_readback' and result['acceptance']['primary_effect']=='reply_draft_saved',result
            assert page.get_by_text('Ответ ещё не опубликован в WB.',exact=True).count()==1
            assert len(posts)==1 and not f.transport.write_calls
            repeat=page.evaluate('async body=>postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,body)',body)
            assert len(posts)==1 and repeat['acceptance']['source_ref']['manual_edit_revision']==2
            mismatch=page.evaluate("async key=>OperatorAcceptance.onceFeedback('feedback_reply',key,'foreign-manual-body',async()=>{throw Error('lost');},{manual_reply_sha256:'foreign'})",job['processing_key'])
            assert mismatch['status']=='unknown' and mismatch['acceptance'] is None,mismatch
            digest=result['acceptance']['source_ref']['manual_reply_sha256']
            approved=page.evaluate('async body=>postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_approve_path,body)',
                dict(processing_key=job['processing_key'],confirmed=True,reply_sha256=digest))
            assert approved['acceptance']['state']=='processing' and len(posts)==2
            f.transport.readbacks=[{'answer':{'text':body['reply']}}];f.worker.run_once();f.worker.run_once()
            assert len(f.transport.write_calls)==1
            page.reload();page.evaluate('()=>{state.feedbacks.server.settingsRevision="fixture";}')
            completed=page.evaluate('async body=>postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_approve_path,body)',
                dict(processing_key=job['processing_key'],confirmed=True,reply_sha256=digest))
            assert len(posts)==2 and completed['acceptance']['external_confirmed'] is True,completed
            # Real selected-complaint form, native queue creation under native lock,
            # then exact per-row proof. No portal thread or browser launch occurs.
            threads=[]
            def spawn(*args,**kwargs):threads.append(kwargs);return SimpleNamespace(start=lambda:None)
            def complaint_lost(route):
                posts.append((route.request.url,route.request.post_data_json))
                response=route.fetch();
                if not 200<=response.status<300:
                    route.fulfill(response=response);raise AssertionError(response.text())
                native=response.json();rid=native['run_id']
                app.feedbacks_complaints_block.submit_jobs.patch(rid,dict(status='success',submitted_count=1,
                    submitted_feedback_ids=['one'],attempts=[dict(feedback_id='one',run_id=rid,attempt_status='submitted',code='row_submit_confirmed_success')]))
                route.abort('failed')
            page.route('**/complaints/submit-selected',complaint_lost)
            page.evaluate("""()=>{state.feedbacks.selectedFeedbackIds=new Set(['one','two']);
                state.feedbacks.rows=[{feedback_id:'one'},{feedback_id:'two'}];state.feedbacks.dateFrom='2026-07-01';state.feedbacks.dateTo='2026-07-20';state.feedbacks.selectedStars=new Set([1]);
                feedbacksAiCompleteForCurrentRows=()=>true;loadFeedbacksComplaints=async()=>{};}""")
            with patch.object(complaint,'admitted_thread',side_effect=spawn),patch.object(complaint,'current_lock_status',return_value={'busy':False}):
                page.evaluate('async()=>submitSelectedFeedbackComplaints()')
                assert len(threads)==1 and len(posts)==3
                assert page.locator('#operator-feedback-receipt .ff-operation-status').inner_text()=='Требует внимания'
                assert page.evaluate('state.feedbacks.submitJob.submittedCount')==1
                page.evaluate("()=>{state.feedbacks.selectedFeedbackIds=new Set(['one','two']);}")
                page.evaluate('async()=>submitSelectedFeedbackComplaints()')
                assert len(posts)==3 and len(threads)==1
            assert not errors,errors
            assert all('native_id=' in u for u in reads) and len(reads)>=5,reads
        finally:browser.close()
    print('operator_feedback_forms_browser_smoke: PASS actual manual edit/approve/lost-response/reload, saved draft != WB, native worker exact readback; complaint selected manifest partial + repeat no resend')

async def two_tab_native_recovery():
    """Original forms/CDP interleaving; native jobs, no portal/provider writes."""
    import asyncio,hashlib
    from playwright.async_api import async_playwright
    with native_server() as (base,app,f,job),patch.object(complaint,'admitted_thread',return_value=SimpleNamespace(start=lambda:None)),patch.object(complaint,'current_lock_status',return_value={'busy':False}):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch();context=await browser.new_context()
            posts=[];reads=[];refusals=[];mode={'hide':True};errors=[]
            def stopped(payload):raise RuntimeError('synthetic provider unavailable before external write')
            async def routes(r):
                path=r.request.url[len(base):].split('?')[0]
                if path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path.endswith('.js') or path.endswith('.css'):
                    await r.continue_();return
                if path=='/v1/sheet-vitrina-v1/operations/feedback':
                    reads.append(r.request.url)
                    if mode['hide']:await r.fulfill(status=404,content_type='application/json',body='{}');return
                    await r.continue_();return
                if path==web.DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH:
                    body=r.request.post_data_json;posts.append(body);response=await r.fetch();data=await response.json()
                    if data.get('run_id'):
                        app.feedbacks_complaints_block.submit_jobs._run(data['run_id'],body,stopped)
                        await r.abort();return
                    # Actual native busy/no source commit is a definitive refusal.
                    assert data.get('not_accepted'),data
                    refusals.append(data)
                    await r.fulfill(response=response);return
                if path in {web.DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH,web.DEFAULT_SHEET_FEEDBACKS_AUTOANSWERS_EDIT_PATH}:
                    if r.request.method=='POST':
                        posts.append(r.request.post_data_json);response=await r.fetch()
                        assert 200<=response.status<300,await response.text()
                        await r.abort();return
                    await r.continue_();return
                await r.fulfill(status=200,content_type='application/json',body='{}')
            await context.route(base+'/**',routes)
            a=await context.new_page();b=await context.new_page()
            for page in (a,b):page.on('pageerror',lambda e:errors.append(str(e)))
            async def load(page):
                await page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                await page.evaluate("""()=>{state.feedbacks.selectedFeedbackIds=new Set(['one','two']);state.feedbacks.rows=[{feedback_id:'one'},{feedback_id:'two'}];state.feedbacks.dateFrom='2026-07-01';state.feedbacks.dateTo='2026-07-20';state.feedbacks.selectedStars=new Set([1]);feedbacksAiCompleteForCurrentRows=()=>true;loadFeedbacksComplaints=async()=>{};}""")
            await load(a);await load(b)
            # Pause A after the original missing-key lookup, inside its lock.
            # A fast native terminal + lost response cannot let stale B POST.
            await a.evaluate("""()=>{const original=Storage.prototype.getItem;Storage.prototype.getItem=function(key){const value=original.call(this,key);if(key.startsWith('operator-complaint-request:')&&!window.racePaused){window.racePaused=true;window.raceRead={key,value};debugger;}return value;};}""")
            session=await context.new_cdp_session(a);await session.send('Debugger.enable')
            paused=asyncio.Event();session.on('Debugger.paused',lambda event:paused.set())
            first=asyncio.create_task(a.evaluate('async()=>submitSelectedFeedbackComplaints()'))
            await asyncio.wait_for(paused.wait(),10)
            second=asyncio.create_task(b.evaluate('async()=>submitSelectedFeedbackComplaints()'))
            # A still owns selected lock; B is queued rather than reading a
            # stale null and assigning an independent UUID.
            await b.wait_for_function("state.feedbacks.submitJob.running===true")
            locks=await b.evaluate('async()=>navigator.locks.query()')
            assert any(lock['name'].startswith('operator-feedback-lock:selected:') for lock in locks['pending']),locks
            assert not posts
            await session.send('Debugger.resume')
            await asyncio.wait_for(asyncio.gather(first,second),20)
            assert len(posts)==1,posts
            identity=posts[0]['request_key'];first_read=await a.evaluate('raceRead')
            assert first_read['value'] is None
            assert await b.evaluate('(key)=>localStorage.getItem(key)',first_read['key'])==identity
            jobs=json.loads(app.feedbacks_complaints_block.submit_jobs.path.read_text())['jobs']
            assert len(jobs)==1 and jobs[0]['request_key']==identity and jobs[0]['status']=='error',jobs
            assert not jobs[0]['submitted_feedback_ids'] and not f.transport.write_calls
            assert all('native_id='+identity in url for url in reads),reads
            # Close first tab, reload second: same source, GET-only even unknown.
            await a.close();before=len(reads);await load(b)
            await b.evaluate('async()=>submitSelectedFeedbackComplaints()')
            assert len(posts)==1 and any('native_id='+identity in url for url in reads[before:])
            assert await b.evaluate('(key)=>localStorage.getItem(key)',first_read['key'])==identity
            mode['hide']=False
            await b.evaluate("state.feedbacks.selectedFeedbackIds=new Set(['one','two'])")
            await b.evaluate('async()=>submitSelectedFeedbackComplaints()')
            assert len(posts)==1
            # Native busy retires ONLY the attempted marker, without treating
            # a foreign active job as this action's accepted receipt.
            foreign=app.handle_sheet_feedbacks_complaints_submit_selected_request(dict(feedback_ids=['foreign'],max_submit=1,date_from='2026-07-01',date_to='2026-07-20',stars=[1],is_answered='all',max_api_rows=50,request_key='foreign-held'),actor='foreign')
            await b.evaluate("state.feedbacks.selectedFeedbackIds=new Set(['three'])")
            await b.evaluate('async()=>submitSelectedFeedbackComplaints()')
            assert len(posts)==2
            assert await b.evaluate('state.feedbacks.error')
            assert refusals[-1]['not_accepted'] is True and not refusals[-1]['run_id']
            assert await b.locator('#operator-feedback-receipt .ff-operation-check').count()==0
            refused=posts[-1]['request_key']
            assert refused!=identity
            assert await b.evaluate('(id)=>Object.keys(localStorage).filter(key=>key.startsWith("operator-feedback-attempt:feedback_complaint:"+id+":"))',refused)==[]
            app.feedbacks_complaints_block.submit_jobs._run(foreign['run_id'],{},stopped)
            await b.evaluate("state.feedbacks.selectedFeedbackIds=new Set(['three'])")
            await b.evaluate('async()=>submitSelectedFeedbackComplaints()')
            assert len(posts)==3 and posts[-1]['request_key']==refused
            jobs=json.loads(app.feedbacks_complaints_block.submit_jobs.path.read_text())['jobs']
            assert len(jobs)==3 and len([r for r in jobs if r['request_key']==identity])==1
            assert await b.evaluate('(key)=>localStorage.getItem(key)',first_read['key'])==identity
            # The shared exact-command lock also prevents two manual POSTs.
            # Native processing_key alone would not prevent two draft revisions.
            editor=await context.new_page();await load(editor)
            for page in (editor,b):await page.evaluate("state.feedbacks.server.settingsRevision='shared-command'")
            await editor.evaluate("""()=>{const original=Storage.prototype.getItem;Storage.prototype.getItem=function(key){const value=original.call(this,key);if(key.startsWith('operator-feedback-attempt:feedback_reply:')&&!window.sharedPaused){window.sharedPaused=true;debugger;}return value;};}""")
            shared_session=await context.new_cdp_session(editor);await shared_session.send('Debugger.enable')
            shared_paused=asyncio.Event();shared_session.on('Debugger.paused',lambda event:shared_paused.set())
            shared_body=dict(processing_key=job['processing_key'],reply='Спасибо за подробный отзыв о товаре.')
            before=len(posts)
            first_edit=asyncio.create_task(editor.evaluate('async body=>postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,body)',shared_body))
            await asyncio.wait_for(shared_paused.wait(),10)
            second_edit=asyncio.create_task(b.evaluate('async body=>postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,body)',shared_body))
            await b.wait_for_function("async()=> (await navigator.locks.query()).pending.some(lock=>lock.name.startsWith('operator-feedback-lock:attempt:'))")
            assert len(posts)==before
            await shared_session.send('Debugger.resume')
            edit_results=await asyncio.wait_for(asyncio.gather(first_edit,second_edit),20)
            assert len(posts)==before+1 and all(r['acceptance']['source_ref']['manual_edit_revision']==2 for r in edit_results)
            assert all(r['acceptance']['primary_effect']=='reply_draft_saved' for r in edit_results)
            await editor.close()
            # Capture the mutable manual body and settings before the first hash
            # await. Actual native draft records the clicked reply, not later data.
            await b.evaluate("""()=>{state.feedbacks.server.settingsRevision='fixture';window.originalDigest=crypto.subtle.digest.bind(crypto.subtle);window.hashHeld=false;const wait=new Promise(resolve=>window.releaseHash=resolve);crypto.subtle.digest=async function(...args){if(!hashHeld){hashHeld=true;await wait;}return originalDigest(...args);};}""")
            manual=dict(processing_key=job['processing_key'],reply='Спасибо за отзыв о товаре.')
            await b.evaluate("""body=>{window.originalActorKey=WEB_VITRINA_CONFIG.user_config_key;window.manual=body;window.manualResult=postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,body);manual.reply='changed after click';state.feedbacks.server.settingsRevision='changed';WEB_VITRINA_CONFIG.user_config_key='changed-after-click';}""",manual)
            await b.evaluate('releaseHash()');result=await b.evaluate('manualResult')
            assert result['acceptance']['primary_effect']=='reply_draft_saved' and posts[-1]['reply']==manual['reply']
            expected_hash=hashlib.sha256(manual['reply'].encode()).hexdigest()
            assert result['acceptance']['source_ref']['manual_reply_sha256']==expected_hash
            await b.evaluate('()=>{crypto.subtle.digest=originalDigest;WEB_VITRINA_CONFIG.user_config_key=originalActorKey;}')
            saved_marker=await b.evaluate("""async body=>{const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify({path:WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,body,expected:{manual_reply_sha256:'HASH'},user:originalActorKey,settings_revision:'fixture'})));const key=Array.from(new Uint8Array(hash)).map(n=>n.toString(16).padStart(2,'0')).join('');return localStorage.getItem('operator-feedback-attempt:feedback_reply:'+body.processing_key+':'+key);}""".replace('HASH',expected_hash),manual)
            assert saved_marker=='attempted'
            # Unsupported lock/storage failure refuse before any new POST.
            before=len(posts)
            await b.evaluate("window.originalLocks=navigator.locks;Object.defineProperty(navigator,'locks',{value:null,configurable:true});state.feedbacks.selectedFeedbackIds=new Set(['four'])")
            await b.evaluate('async()=>submitSelectedFeedbackComplaints()')
            assert 'между вкладками' in await b.evaluate('state.feedbacks.error') and len(posts)==before
            await b.evaluate("Object.defineProperty(navigator,'locks',{value:originalLocks,configurable:true});window.originalGet=Storage.prototype.getItem;Storage.prototype.getItem=function(key){if(key.startsWith('operator-complaint-request:')||key.startsWith('operator-feedback-attempt:'))throw Error('blocked storage');return originalGet.call(this,key);};state.feedbacks.selectedFeedbackIds=new Set(['four'])")
            await b.evaluate('async()=>submitSelectedFeedbackComplaints()')
            assert 'сохранить номер' in await b.evaluate('state.feedbacks.error') and len(posts)==before
            failure=await b.evaluate("""async body=>{try{await postAutoanswers(WEB_VITRINA_CONFIG.feedbacks_autoanswers_edit_path,{...body,reply:'Новый несохранённый ответ.'});}catch(error){return error.message;}}""",manual)
            assert 'сохранить номер' in failure and len(posts)==before
            await b.evaluate('()=>{Storage.prototype.getItem=originalGet;}')
            assert not errors,errors
            assert not f.transport.write_calls
            await browser.close()
    print('operator_feedback_forms_browser_smoke: PASS actual CDP two-tab/source terminal/lost reply/unknown GET, closed-tab exact recovery, native busy retry, shared manual once/source revision, immutable manual operands, lock/storage pre-POST guards')

if __name__=='__main__':
    import asyncio
    main();asyncio.run(two_tab_native_recovery())
