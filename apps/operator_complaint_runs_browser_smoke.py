#!/usr/bin/env python3
"""Actual Chromium manual button, native HTTP/source and synthetic worker."""
import sys,json
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from apps.operator_complaint_runs_http_smoke import server,READ,op,http,worker,hold,native

def queued_snapshot_forms():
    """Actual forms wait on another tab's lock without changing clicked intent."""
    for kind in ('schedule','run'):
        with server() as (base,app,block,feedbacks),patch.object(native,'admitted_thread',side_effect=hold) as spawned,sync_playwright() as pw:
            browser=pw.chromium.launch()
            try:
                context=browser.new_context();page=context.new_page();holder=context.new_page();posts=[];errors=[]
                def routes(route):
                    path=route.request.url[len(base):].split('?')[0]
                    if path in {http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,op.PATH,READ,http.DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH} or path.startswith('/v1/sheet-vitrina-v1/operations/') or path.endswith(('.js','.css')):route.continue_()
                    else:route.fulfill(status=200,content_type='application/json',body='{}')
                context.route(base+'/**',routes)
                def observe(route):
                    posts.append(route.request.post_data_json);response=route.fetch()
                    assert response.status==(200 if kind=='schedule' else 202),response.text()
                    route.fulfill(response=response)
                path=http.DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH if kind=='schedule' else op.PATH
                def on_target(route):
                    if route.request.method=='POST':observe(route)
                    else:route.continue_()
                context.route(base+path,on_target)
                for tab in (page,holder):
                    tab.on('pageerror',lambda e:errors.append(str(e)));tab.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
                    tab.evaluate('async()=>{activateUnifiedTab("feedbacks");activateFeedbacksSubsection("automation");while(state.feedbacks.automation.loading)await new Promise(r=>setTimeout(r,10));if(!state.feedbacks.automation.loaded)await loadFeedbacksAutomation();}')
                if kind=='schedule':page.evaluate('()=>{state.feedbacks.automation.schedules[0].local_time_hhmm="13:37";state.feedbacks.automation.dirty=true;renderFeedbacksAutomationPanel();}')
                clicked_revision=page.evaluate('state.feedbacks.automation.sourceRevision')
                key='complaintScheduleFenceKey("save")' if kind=='schedule' else 'complaintRunFenceKey()'
                holder.evaluate('()=>{window.held=navigator.locks.request('+key+',()=>new Promise(resolve=>{window.releaseLock=resolve;}));}')
                holder.wait_for_function('typeof releaseLock==="function"')
                if kind=='run':
                    page.evaluate('()=>{const original=crypto.subtle.digest.bind(crypto.subtle);crypto.subtle.digest=(...args)=>new Promise(resolve=>{window.releaseDigest=()=>original(...args).then(resolve);});}')
                action='saveFeedbacksAutomationSchedules()' if kind=='schedule' else 'runFeedbacksAutomationNow()'
                page.evaluate('()=>{window.clicked='+action+';}')
                page.evaluate('()=>{const a=state.feedbacks.automation;a.schedules[0].id="later_unsaved";a.schedules[0].local_time_hhmm="22:22";a.sourceRevision="f".repeat(64);a.dirty=true;}')
                holder.evaluate('releaseLock()');holder.evaluate('held')
                if kind=='run':
                    page.wait_for_function('typeof releaseDigest==="function"')
                    page.evaluate('()=>{state.feedbacks.automation.schedules[0].id="still_later";releaseDigest();}')
                page.evaluate('clicked');assert len(posts)==1,posts
                assert posts[0]['expected_source_revision']==clicked_revision,posts
                if kind=='schedule':
                    assert posts[0]['schedules'][0]['id']=='one' and posts[0]['schedules'][0]['local_time_hhmm']=='13:37',posts
                    assert block.store.read()['schedules'][0]['local_time_hhmm']=='13:37'
                    assert spawned.call_count==0 and not feedbacks.reads
                else:
                    assert posts[0]['schedule_id']=='one',posts
                    receipt=op.read(posts[0]['operation_id'],op.RunScope.from_entrypoint(app,actor='local_operator'))['acceptance']
                    assert receipt['durable_saved'] and receipt['state']=='processing' and not receipt['external_confirmed']
                    assert spawned.call_count==1 and not feedbacks.reads
                assert not errors,errors
            finally:browser.close()
    print('queued_snapshot_forms: PASS actual schedule/manual forms, held cross-tab WebLock and digest await, native clicked operands preserved')


def main():
    with server() as (base,app,block,feedbacks),patch.object(native,'admitted_thread',side_effect=hold) as spawned,sync_playwright() as pw:
        browser=pw.chromium.launch()
        try:
            context=browser.new_context();page=context.new_page();other=context.new_page();posts=[];reads=[];errors=[]
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path in {http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,op.PATH,READ,http.DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH} or path.startswith('/v1/sheet-vitrina-v1/operations/') or path.endswith(('.js','.css')):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            context.route(base+'/**',routes)
            def missing(route):reads.append(route.request.url);route.abort('failed')
            context.route('**/feedbacks/automation/run?operation_id=*',missing)
            def lost(route):
                posts.append(route.request.post_data_json);response=route.fetch();assert response.status==202,response.text()
                # Unknown 400 after the native source committed is ambiguous.
                route.fulfill(status=400,content_type='application/json',body=json.dumps({'error':'synthetic ambiguous response','code':'unknown'}))
            context.route('**/feedbacks/automation/run-now',lost)
            def init(tab):
                tab.on('pageerror',lambda error:errors.append(str(error)));tab.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
                tab.evaluate('async()=>{activateUnifiedTab("feedbacks");activateFeedbacksSubsection("automation");while(state.feedbacks.automation.loading)await new Promise(r=>setTimeout(r,10));if(!state.feedbacks.automation.loaded)await loadFeedbacksAutomation();}')
            init(page);init(other)
            page.locator('[data-feedbacks-auto-run-now]').click()
            page.wait_for_function('()=>!state.feedbacks.automation.running')
            assert len(posts)==1 and spawned.call_count==1 and not feedbacks.reads
            identity=posts[0]['operation_id'];saved=op.read(identity,op.RunScope.from_entrypoint(app,actor='local_operator'))
            assert saved['acceptance']['state']=='processing'
            assert page.locator('[data-feedbacks-auto-run-now]').is_disabled()
            assert page.locator('#operator-complaint-run-receipt .ff-operation-status').count()==0
            record=page.evaluate('()=>JSON.parse(localStorage.getItem(complaintRunFenceKey()))');assert set(record)=={'identity','digest'}
            # Second tab programmatic intent has the same GET-only fence. No
            # second mutation, including after closure of the original tab.
            other.evaluate('async()=>await runFeedbacksAutomationNow()');assert len(posts)==1
            page.close();replacement=context.new_page();init(replacement)
            assert len(posts)==1 and replacement.locator('[data-feedbacks-auto-run-now]').is_disabled()
            context.unroute('**/feedbacks/automation/run?operation_id=*',missing)
            replacement.evaluate('async()=>await onceComplaintRun(true)')
            assert replacement.locator('#operator-complaint-run-receipt h3').inner_text()=='Принято'
            assert replacement.get_by_text('Задание сохранено.',exact=True).count()==1
            assert not replacement.evaluate('()=>complaintRunPending()') and len(posts)==1
            other.wait_for_function('()=>!!state.feedbacks.automation.nativeRunActive')
            other.evaluate('async()=>await runFeedbacksAutomationNow()');assert len(posts)==1
            worker(block,saved);assert feedbacks.reads==1
            completed=replacement.evaluate('async record=>{const receipt=await readComplaintRunReceipt(record);showComplaintRunReceipt(receipt);return receipt;}',record)
            assert completed['state']=='completed' and completed['external_confirmed'] is False
            assert replacement.get_by_text('Обработано',exact=True).count()==1
            hostile=dict(completed,actor='foreign');assert not replacement.evaluate('data=>complaintRunMatches(data.receipt,data.record)',{'receipt':hostile,'record':record})
            hostile=dict(completed,operation_id='complaint-run:'+'f'*32);assert not replacement.evaluate('data=>complaintRunMatches(data.receipt,data.record)',{'receipt':hostile,'record':record})
            replacement.evaluate('async()=>await refreshFeedbacksAutomationState()')
            # Actual native maintenance refusal may unlock this attempted intent;
            # a lost maintenance response remains unknown and must not resubmit.
            from packages.application.business_data_write_barrier import acquire_barrier
            acquire_barrier(block.runtime_dir,window_id='complaint-browser-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'b'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
            context.unroute('**/feedbacks/automation/run-now',lost)
            def known(route):posts.append(route.request.post_data_json);response=route.fetch();assert response.status==423;route.fulfill(response=response)
            context.route('**/feedbacks/automation/run-now',known)
            replacement.evaluate('()=>document.querySelector("#operator-complaint-run-receipt").close()')
            replacement.evaluate('async()=>await runFeedbacksAutomationNow()')
            assert len(posts)==2 and not replacement.evaluate('()=>complaintRunPending()')
            assert replacement.evaluate('state.feedbacks.automation.error')
            context.unroute('**/feedbacks/automation/run-now',known)
            def lost_barrier(route):posts.append(route.request.post_data_json);response=route.fetch();assert response.status==423;route.abort('failed')
            context.route('**/feedbacks/automation/run-now',lost_barrier)
            replacement.evaluate('async()=>await runFeedbacksAutomationNow()');assert len(posts)==3 and replacement.evaluate('()=>complaintRunPending()')
            replacement.reload();replacement.evaluate('async()=>await onceComplaintRun(true)');assert len(posts)==3 and replacement.evaluate('()=>complaintRunPending()')
            assert spawned.call_count==1 and len(block.store.read()['runs'])==1 and not errors,errors
            assert reads and all('operation_id=' in value for value in reads)
            hostile_actor='</script><script>window.actorInjected=true</script>'
            safe=context.new_page();safe.on('pageerror',lambda error:errors.append(str(error)))
            with patch.object(http,'_current_web_user_actor',return_value=hostile_actor):safe.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
            assert safe.evaluate('WEB_VITRINA_CONFIG.complaint_run_actor')==hostile_actor
            assert safe.evaluate('window.actorInjected===undefined') and not errors,errors
        finally:browser.close()
    print('operator_complaint_runs_browser_smoke: PASS actual button/unknown400 after save/closed-tab+two-tab GET-only/same-ID durable green/native terminal != WB/hostile identity/received423 vs lost423/no duplicate')
if __name__=='__main__':main();queued_snapshot_forms()
