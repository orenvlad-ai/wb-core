"""Real native SPP + HTTP/Chromium recovery with synthetic WB providers only."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
import unittest
from uuid import uuid4
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from apps.wb_spp_tester_browser_smoke import _LocalSppUiServer, _open_manual_panel
from apps.wb_spp_tester_smoke import PRIMARY_NM, _payload
from packages.application import operator_spp_jobs as operator
from packages.application.wb_spp_tester import WbSppTesterError
from packages.application.operator_operations import journal, read_acceptance
from packages.application.change_registry import ATTEMPT_EVENTS_TABLE
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


def body(prices=(810,),identity=None):
    return dict(_payload(prices),request_id=identity or 'spp-start:'+uuid4().hex)


def scope(server,actor='native-a',human='principal-a'):
    return operator.SppScope.from_entrypoint(server.entrypoint,native_actor=actor,actor=human)


def file_digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def _spp_tab_metadata(page):
    """Passive bounded metadata only; never read a response or request body."""
    metadata={'network':[],'pageerrors':[],'listener_errors':[]}
    def network(kind,event):
        try:
            if len(metadata['network'])>=100:return
            request=event.request if kind=='response' else event
            url=urlsplit(request.url)
            metadata['network'].append(dict(monotonic=time.monotonic(),kind=kind,
                method=request.method,path=url.path[:200],has_query=bool(url.query),
                status=event.status if kind=='response' else None,
                failure=(request.failure or '')[:200] if kind=='requestfailed' else None))
        except Exception as error:
            if len(metadata['listener_errors'])<10:metadata['listener_errors'].append(str(error)[:200])
    def pageerror(error):
        if len(metadata['pageerrors'])<20:metadata['pageerrors'].append(str(error)[:500])
    page.on('request',lambda request:network('request',request))
    page.on('response',lambda response:network('response',response))
    page.on('requestfailed',lambda request:network('requestfailed',request))
    page.on('pageerror',pageerror)
    return metadata


def _spp_start_timeout_diagnostic(page,tab_number,metadata):
    # This is a post-timeout observation, not proof of an earlier UI state.
    diagnostic=dict(phase='POST_TIMEOUT',tab_number=tab_number,
        recorded_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),metadata=metadata)
    try:
        diagnostic['snapshot']=page.evaluate('''() => {
            const node=document.querySelector('[data-spp-test-start]');
            const s=typeof state==='undefined'?null:state.prices.sppTest;
            const buyer=s?.buyerSession||{};
            const parsed=collectSppTestPrices();
            const quarantinePlan=sppQuarantinePlan();
            const job=s?.job||{},activeJob=s?.activeJob||{};
            const active=!!activeJob.job_id || ['preflight','measuring','cooldown','restoring','running'].indexOf(String(job.status||''))!==-1;
            return {phase:'POST_TIMEOUT',timestamp_ms:Date.now(),performance_ms:performance.now(),
                visibility:document.visibilityState,hasFocus:document.hasFocus(),
                disabled:node?.disabled,selected_nm:document.querySelector('[data-spp-test-nm]')?.value,
                prices:Array.from(document.querySelectorAll('[data-spp-test-price-index]')).slice(0,6)
                    .map(n=>({index:n.dataset.sppTestPriceIndex,value:n.value,connected:n.isConnected})),
                reason:document.querySelector('[data-spp-test-start-reason]')?.innerText.slice(0,500),
                buyer_label:document.querySelector('[data-wb-buyer-session-state]')?.innerText.slice(0,500),
                error:document.querySelector('[data-spp-test-error]')?.innerText.slice(0,500),
                operands:s?{prices:s.prices.slice(0,6),selected_nm:s.selectedNmId,pending:s.pending,
                    operator_scope_present:!!s.operatorScope,write_enabled:!!state.prices.writeEnabled,
                    active_job_present:!!s.activeJob?.job_id,job_status:s.job?.status,
                    active:active,parsed_valid:parsed.valid,parsed_reason:String(parsed.reason||'').slice(0,500),
                    quarantine_valid:quarantinePlan.valid,quarantine_reason:String(quarantinePlan.reason||'').slice(0,500),
                    quarantine_risk_count:(quarantinePlan.risks||[]).length,
                    start_loading:s.startLoading,status_loading:s.statusLoading,
                    buyer_loading:s.buyerSessionLoading,buyer_valid:buyer.valid===true,
                    buyer_capability_valid:buyer.capability_valid===true}:null};
        }''')
    except Exception as error:
        diagnostic['snapshot_error']=dict(type=type(error).__name__,message=str(error)[:500])
    try:
        print('spp_start_timeout_diagnostic: '+json.dumps(diagnostic,ensure_ascii=False),file=sys.stderr,flush=True)
    except Exception:
        # Even an unavailable diagnostic sink must retain the original timeout.
        pass


@contextmanager
def fixture():
    with patch.dict(os.environ,{'WB_CORE_WEB_AUTH_REQUIRED':'0'}),_LocalSppUiServer() as server:
        yield server


class Native(unittest.TestCase):
    def test_accept_before_preflight_and_same_id_race(self):
        with fixture() as f:
            command=body();sc=scope(f);entered=threading.Event();release=threading.Event();count=[];errors=[]
            original=f.spp_block._require_buyer_session
            def held():
                count.append(1);entered.set();release.wait(5);return original()
            def run():
                try:f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
                except Exception as e:errors.append(e)
            with patch.object(f.spp_block,'_require_buyer_session',side_effect=held):
                thread=threading.Thread(target=run);thread.start();self.assertTrue(entered.wait(5))
                path=f.spp_block._job_path(operator.job_id(command['request_id'],sc));before=file_digest(path)
                with patch.object(f.spp_block,'_capture_baseline',side_effect=AssertionError('GET performed provider read')):
                    recovered=f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)
                self.assertEqual(file_digest(path),before);self.assertEqual(recovered['acceptance']['state'],'processing')
                duplicate=f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
                self.assertTrue(duplicate['recovered']);self.assertEqual(len(count),1)
                with self.assertRaises(WbSppTesterError) as conflict:
                    f.spp_block.start(dict(command,prices=[800]),actor=sc.native_actor,operator_scope=sc)
                self.assertEqual(conflict.exception.http_status,409)
                release.set();thread.join(5);self.assertFalse(thread.is_alive());self.assertFalse(errors,errors)
            result=f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)
            self.assertEqual(result['acceptance']['state'],'completed',result)
            uploads=len(f.spp_prices_source.upload_payloads)
            f.spp_block.safety=replace(f.spp_block.safety,prices_write_enabled=False)
            self.assertTrue(f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc)['recovered'])
            self.assertEqual(len(f.spp_prices_source.upload_payloads),uploads)

    def test_scopes_immutable_source_and_foreign_restore(self):
        with fixture() as f:
            sc=scope(f);command=body();result=f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            for foreign in (replace(sc,actor='other'),replace(sc,native_actor='other'),replace(sc,seller_id='other'),replace(sc,account_scope='other')):
                self.assertIsNone(operator.read_job(foreign,command['request_id']))
                self.assertEqual(operator.items(selected={operator.DOMAIN},scope=foreign),[])
                self.assertEqual(f.spp_block.history({},operator_scope=foreign)['items'][0]['job_id'],'11111111111111111111111111111111')
                self.assertIsNone(f.spp_block.status({'job_id':result['job']['job_id']},operator_scope=foreign,reconcile=False)['job'])
                with self.assertRaises(WbSppTesterError) as denied:
                    f.spp_block.restore(dict(job_id=result['job']['job_id'],confirm_restore=True),actor=foreign.native_actor,operator_scope=foreign)
                self.assertEqual(denied.exception.http_status,404)
            saved=f.spp_block._load_job(result['job']['job_id']);saved['baseline']['price']+=1
            with self.assertRaises(ValueError):f.spp_block._save_job(saved)
            saved=f.spp_block._load_job(result['job']['job_id']);saved['input']['target_prices']=[700]
            with self.assertRaises(ValueError):f.spp_block._save_job(saved)
            stored=operator.read_job(sc,command['request_id']);self.assertEqual(stored['input']['target_prices'],[810])

    def test_postcommit_preflight_rejection_is_retained(self):
        with fixture() as f:
            sc=scope(f);command=body([599.4])
            with self.assertRaises(WbSppTesterError) as rejected:
                f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            self.assertEqual(rejected.exception.http_status,422)
            self.assertTrue(rejected.exception.payload['acceptance']['durable_saved'])
            result=f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)
            self.assertEqual(result['job']['status'],'preflight_rejected');self.assertEqual(result['acceptance']['state'],'needs_attention')
            self.assertEqual(f.spp_prices_source.upload_payloads,[])
            self.assertTrue(f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc)['recovered'])
            self.assertEqual(f.spp_prices_source.upload_payloads,[])

    def test_crash_after_acceptance_does_not_restart(self):
        class SimulatedDeath(BaseException):pass
        with fixture() as f:
            sc=scope(f);command=body()
            with patch.object(f.spp_block,'_require_buyer_session',side_effect=SimulatedDeath()):
                with self.assertRaises(SimulatedDeath):f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            saved=operator.read_job(sc,command['request_id']);self.assertFalse(saved['baseline']);self.assertFalse(f.spp_prices_source.upload_payloads)
            with patch.object(f.spp_block,'_run_job',side_effect=AssertionError('recovery restarted worker')):
                self.assertTrue(f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc)['recovered'])
                self.assertTrue(f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)['recovered'])
            # A distinct explicit new Start owns native orphan retirement, never GET.
            next_command=body([800]);f.spp_block.start(next_command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            self.assertEqual(operator.read_job(sc,command['request_id'])['status'],'preflight_rejected')

    def test_crash_after_baseline_keeps_native_explicit_restore_only(self):
        with fixture() as f:
            sc=scope(f);command=body()
            with patch.object(f.spp_block,'_start_background_job',side_effect=RuntimeError('synthetic thread admission failure')):
                with self.assertRaises(RuntimeError):f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=True)
            before=list(f.spp_prices_source.upload_payloads)
            result=f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)
            self.assertEqual(result['acceptance']['state'],'needs_attention');self.assertTrue(result['acceptance']['recovery_restore_available'])
            self.assertFalse(result['acceptance']['interrupted_before_write']);self.assertEqual(before,f.spp_prices_source.upload_payloads)
            # The legacy busy-start envelope cannot expose a foreign typed source.
            with patch.object(f.spp_block,'_acquire_execution_lock',return_value=None),self.assertRaises(WbSppTesterError) as blocked:
                f.spp_block.start(_payload([810]),actor='other',operator_scope=replace(sc,native_actor='other',actor='other'))
            self.assertIsNone(blocked.exception.payload['active_job'])
            with self.assertRaises(WbSppTesterError) as malformed:
                f.spp_block.status({'request_id':[command['request_id'],command['request_id']]},operator_scope=sc)
            self.assertEqual(malformed.exception.http_status,422)
            with patch.object(f.spp_block,'_run_job',side_effect=AssertionError('restart on recovery')):
                self.assertTrue(f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc)['recovered'])
            f.spp_block.restore(dict(job_id=result['job']['job_id'],confirm_restore=True),actor=sc.native_actor,operator_scope=sc)
            proof=f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)
            self.assertTrue(proof['acceptance']['restoration_confirmed']);self.assertFalse(proof['acceptance']['external_confirmed'])
            self.assertEqual(before,f.spp_prices_source.upload_payloads)


    def test_corrupt_retained_proof_is_unknown_not_rejected(self):
        with fixture() as f:
            sc=scope(f);command=body();f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            path=f.spp_block._job_path(operator.job_id(command['request_id'],sc));value=json.loads(path.read_text());value['operator_acceptance']['proof_digest']='bad';path.write_text(json.dumps(value))
            uploads=len(f.spp_prices_source.upload_payloads)
            with self.assertRaises(ValueError) as error:f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc)
            self.assertNotIsInstance(error.exception,operator.Rejected)
            self.assertEqual(len(f.spp_prices_source.upload_payloads),uploads)

    def test_capacity_before_acceptance_and_no_retention_eviction(self):
        with fixture() as f:
            sc=scope(f);command=body();result=f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            with patch.object(operator,'MAX_JOBS',2),patch.object(f.spp_block,'_require_buyer_session',side_effect=AssertionError('capacity started preflight')):
                with self.assertRaises(WbSppTesterError) as error:f.spp_block.start(body(),actor=sc.native_actor,operator_scope=sc)
                self.assertEqual(error.exception.payload['code'],'spp_start_capacity_exceeded')
                self.assertTrue(f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc)['recovered'])
            self.assertEqual(operator.read_job(sc,command['request_id'])['job_id'],result['job']['job_id'])

    def test_common_journal_scope_count_search_detail_and_flags_only(self):
        with fixture() as f:
            sc=scope(f);command=body();result=f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            opts=dict(allowed_domains={operator.DOMAIN},spp_scope=sc)
            view=journal(f.runtime.db_path,domain=operator.DOMAIN,**opts);self.assertEqual(view['total'],1)
            self.assertEqual(view['items'][0]['state'],'completed')
            self.assertEqual(read_acceptance(f.runtime.db_path,command['request_id'],**opts)['operation_id'],command['request_id'])
            self.assertEqual(journal(f.runtime.db_path,search='wrong-id',**opts)['total'],0)
            other=dict(opts,spp_scope=replace(sc,actor='other'))
            self.assertEqual(journal(f.runtime.db_path,search=command['request_id'],**other)['total'],0)
            self.assertIsNone(read_acceptance(f.runtime.db_path,command['request_id'],**other))
            # Leave native terminal flags intact, remove exact external evidence.
            import sqlite3
            conn=sqlite3.connect(f.runtime.db_path)
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(ATTEMPT_EVENTS_TABLE,)).fetchall():conn.execute('DROP TRIGGER '+row[0])
            conn.execute(f'DELETE FROM {ATTEMPT_EVENTS_TABLE}');conn.commit();conn.close()
            proof=f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)
            self.assertEqual(proof['job']['status'],'complete');self.assertEqual(proof['acceptance']['state'],'needs_attention')
            self.assertFalse(proof['job']['operator_verified_complete']);self.assertFalse(proof['job']['measurements'][0]['registry_confirmed'])
            self.assertFalse(f.spp_block.history({},operator_scope=sc)['items'][0]['operator_verified_complete'])

    def test_every_native_measurement_and_restore_child_is_required(self):
        with fixture() as f:
            sc=scope(f);command=body([810,800]);calls=[]
            original=f.spp_block._fresh_measurement_write_guard
            def blocked_second(*args,**kwargs):
                calls.append(True)
                if len(calls)==2:return dict(safe=False,status='seller_guard_unavailable',reason='synthetic second point blocked')
                return original(*args,**kwargs)
            with patch.object(f.spp_block,'_fresh_measurement_write_guard',side_effect=blocked_second):
                f.spp_block.start(command,actor=sc.native_actor,operator_scope=sc,run_async=False)
            partial=operator.read_job(sc,command['request_id'])
            self.assertEqual(partial['measurements'][0]['status'],'ok');self.assertIsNone(partial['measurements'][1]['uploadID'])
            self.assertTrue(operator.restore_verified(partial))
            partial['status']='complete';partial['result_status']='success';partial['measurements'][1]['status']='ok'
            f.spp_block._save_job(partial)
            path=f.spp_block._job_path(partial['job_id']);before=file_digest(path);db_before=file_digest(sc.db_path)
            for read in (lambda:f.spp_block.status({'request_id':command['request_id']},operator_scope=sc)['acceptance'],
                         lambda:journal(sc.db_path,allowed_domains={operator.DOMAIN},spp_scope=sc)['items'][0]):
                receipt=read();self.assertEqual(receipt['state'],'needs_attention');self.assertFalse(receipt['external_confirmed'])
            self.assertEqual(file_digest(path),before);self.assertEqual(file_digest(sc.db_path),db_before)
            # Even invented positive outcome hints cannot supply the absent
            # native stage. The command and immutable preflight stay intact.
            missing=json.loads(json.dumps(partial));missing['measurements'][1]['uploadID']=12345
            missing['measurements'][1]['evidence']['prewrite_guard']['safe']=True
            self.assertFalse(operator.external_proof(missing,sc))

            complete_body=body([810,800]);f.spp_block.start(complete_body,actor=sc.native_actor,operator_scope=sc,run_async=False)
            complete=operator.read_job(sc,complete_body['request_id'])
            self.assertEqual(operator.public(complete,scope=sc)['state'],'completed')
            variants={}
            for name in ('missing','reordered','duplicate','blocked','missing_restore','blocked_restore'):
                changed=json.loads(json.dumps(complete))
                if name=='missing':changed['measurements'].pop()
                elif name=='reordered':changed['measurements'].reverse()
                elif name=='duplicate':changed['measurements'][1]['point_id']=changed['measurements'][0]['point_id']
                elif name=='blocked':changed['measurements'][1]['evidence']['prewrite_guard']['safe']=False
                elif name=='missing_restore':changed['restore']['steps']=[]
                else:changed['restore']['steps'][0]['upload']['uploadID']=None
                variants[name]=changed
            for name,changed in variants.items():
                with self.subTest(name=name):
                    self.assertFalse(operator.external_proof(changed,sc))
                    receipt=operator.public(changed,scope=sc)
                    self.assertEqual(receipt['state'],'needs_attention');self.assertFalse(receipt['external_confirmed'])
            # Preserve the native no-write restore path when the fully proved
            # measurement itself already leaves the exact baseline tuple.
            baseline_body=body([complete['baseline']['discountedPrice']])
            f.spp_block.start(baseline_body,actor=sc.native_actor,operator_scope=sc,run_async=False)
            baseline=operator.read_job(sc,baseline_body['request_id'])
            self.assertEqual(baseline['restore']['steps'],[])
            self.assertEqual(operator.public(baseline,scope=sc)['state'],'completed')

    def test_http_unknown_recovery_is_query_only(self):
        with fixture() as f,sync_playwright() as pw:
            api=pw.request.new_context();command=body()
            reply=api.post(f.base_url+'/v1/sheet-vitrina-v1/prices/spp-test/start',data=command)
            self.assertEqual(reply.status,200,reply.text());result=reply.json()
            self.assertFalse(result['recovered'])
            job_id=result['job']['job_id'];deadline=time.monotonic()+5
            while f.spp_block._load_job(job_id)['status'] in ('preflight','measuring','restoring') and time.monotonic()<deadline:time.sleep(.01)
            path=f.spp_block._job_path(job_id);before=file_digest(path);uploads=len(f.spp_prices_source.upload_payloads)
            with patch.object(f.spp_block,'_reconcile_current_job',side_effect=AssertionError('GET reconciled')),patch.object(f.spp_block,'_capture_baseline',side_effect=AssertionError('GET provider')):
                recovered=api.get(f.base_url+operator.PATH+'?request_id='+command['request_id'])
                self.assertEqual(recovered.status,200,recovered.text());self.assertTrue(recovered.json()['recovered'])
                self.assertEqual(api.get(f.base_url+operator.PATH+'?request_id='+command['request_id']+'&request_id=bad').status,422)
            self.assertEqual(before,file_digest(path));self.assertEqual(uploads,len(f.spp_prices_source.upload_payloads))
            self.assertEqual(api.get(f.base_url+'/v1/sheet-vitrina-v1/operations?domain=spp_test_jobs').json()['total'],1)
            api.dispose()

    def test_chromium_unknown400_failed_get_two_tabs_closed_tab(self):
        with fixture() as f,sync_playwright() as pw:
            browser=pw.chromium.launch();context=browser.new_context(viewport={'width':1440,'height':940});pages=[context.new_page(),context.new_page()]
            tab_metadata=[_spp_tab_metadata(page) for page in pages]
            posts=[];saved=[];get_fail=[True]
            def intercept(route):
                request=route.request
                if request.method=='POST' and request.url.endswith('/spp-test/start'):
                    posts.append(request.post_data_json)
                    reply=route.fetch();self.assertEqual(reply.status,200,reply.text());saved.append(reply.json())
                    route.fulfill(status=400,content_type='application/json',body=json.dumps({'error':'synthetic unknown reply'}))
                elif '/spp-test/status?request_id=' in request.url and get_fail[0]:route.abort()
                else:route.continue_()
            context.route('**/*',intercept)
            for page in pages:
                _open_manual_panel(page,f.base_url);page.locator('[data-spp-test-price-index="0"]').fill('810')
                try:
                    page.wait_for_function('() => document.querySelector("[data-spp-test-start]").disabled === false')
                except PlaywrightTimeoutError:
                    try:
                        _spp_start_timeout_diagnostic(page,pages.index(page)+1,tab_metadata[pages.index(page)])
                    except Exception as diagnostic_error:
                        try:print('spp_start_timeout_diagnostic_error: '+str(diagnostic_error)[:500],file=sys.stderr,flush=True)
                        except Exception:pass
                    raise
            # Dispatch both true click events before either async admission reply.
            for page in pages:page.locator('[data-spp-test-start]').evaluate('(node)=>node.click()')
            pages[0].wait_for_function('() => document.querySelector("[data-spp-test-error]").innerText.includes("повторная отправка")',timeout=10000)
            self.assertEqual(len(posts),1);identity=posts[0]['request_id'];self.assertEqual(posts[0]['prices'],[810])
            for page in pages:self.assertTrue(page.locator('[data-spp-test-start]').is_disabled())
            for page in pages:page.close()
            get_fail[0]=False
            reopened=context.new_page();_open_manual_panel(reopened,f.base_url)
            reopened.wait_for_function('() => document.querySelector("[data-spp-test-acceptance]").innerText.includes("Задание сохранено")',timeout=10000)
            reopened.locator('[data-spp-test-read]').click();self.assertEqual(len(posts),1)
            recovered=context.request.get(f.base_url+operator.PATH+'?request_id='+identity).json()
            self.assertEqual(recovered['job']['job_id'],saved[0]['job']['job_id']);self.assertTrue(recovered['recovered'])
            self.assertEqual(recovered['acceptance']['state'],'completed',recovered)
            self.assertTrue(reopened.locator('[data-spp-test-start]').is_disabled())
            reopened.locator('[data-spp-test-new]').wait_for(state='visible');reopened.locator('[data-spp-test-new]').click()
            reopened.locator('[data-spp-test-price-index="0"]').fill('800')
            reopened.wait_for_function('() => document.querySelector("[data-spp-test-start]").disabled === false')
            reopened.locator('[data-spp-test-start]').click()
            reopened.wait_for_function('() => document.querySelector("[data-spp-test-acceptance]").innerText.includes("Задание сохранено")')
            self.assertEqual(len(posts),2);self.assertNotEqual(posts[1]['request_id'],identity)
            context.close();browser.close()

    def test_actual_authenticated_http_grants_and_foreign_identity(self):
        from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
        password='synthetic-SPP-only'
        env=dict(WB_CORE_WEB_AUTH_REQUIRED='1',WB_CORE_WEB_AUTH_USERNAME='owner',WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(password),WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-SPP-only')
        with patch.dict(os.environ,env),_LocalSppUiServer() as f,sync_playwright() as pw:
            for name,sections in [('foreign',['prices','sku_management']),('denied',['ads'])]:
                f.entrypoint.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-'+name,username=name,display_name=name,role='operator',allowed_sections=sections,manage_users=False,password_hash=_password_hash(password),is_active=True,created_at='2026-07-20T12:00:00Z',updated_at='2026-07-20T12:00:00Z'))
            def login(name):
                api=pw.request.new_context();reply=api.post(f.base_url+'/login',form=dict(username=name,password=password,next='/sheet-vitrina-v1/operations'));self.assertEqual(reply.status,200);return api
            owner,foreign,denied=(login(name) for name in ('owner','foreign','denied'))
            command=body();path='/v1/sheet-vitrina-v1/prices/spp-test/start'
            reply=owner.post(f.base_url+path,data=command);self.assertEqual(reply.status,200,reply.text());saved=reply.json()
            self.assertEqual(saved['acceptance']['actor'],'owner');self.assertFalse(saved['recovered'])
            self.assertEqual(foreign.get(f.base_url+operator.PATH+'?request_id='+command['request_id']).status,404)
            self.assertEqual(denied.get(f.base_url+operator.PATH+'?request_id='+command['request_id']).status,403)
            self.assertEqual(foreign.get(f.base_url+operator.PATH+'?job_id='+saved['job']['job_id']).json()['job'],None)
            self.assertEqual(foreign.post(f.base_url+'/v1/sheet-vitrina-v1/prices/spp-test/restore',data=dict(job_id=saved['job']['job_id'],confirm_restore=True)).status,404)
            self.assertEqual(foreign.get(f.base_url+'/v1/sheet-vitrina-v1/operations?domain=spp_test_jobs&search='+command['request_id']).json()['total'],0)
            self.assertEqual(foreign.get(f.base_url+'/v1/sheet-vitrina-v1/operations/'+command['request_id']).status,404)
            self.assertEqual(owner.get(f.base_url+operator.PATH+'?request_id='+command['request_id']).status,200)
            duplicate=owner.post(f.base_url+path,data=command);self.assertEqual(duplicate.status,200);self.assertTrue(duplicate.json()['recovered'])
            deadline=time.monotonic()+5
            while f.spp_block._load_job(saved['job']['job_id'])['status']!='complete' and time.monotonic()<deadline:time.sleep(.01)
            first_path=f.spp_block._job_path(saved['job']['job_id']);first_bytes=file_digest(first_path)
            # Same browser ID belongs independently to each server principal scope.
            second=foreign.post(f.base_url+path,data=command);self.assertEqual(second.status,200,second.text());second_job=second.json()
            self.assertFalse(second_job['recovered']);self.assertEqual(second_job['acceptance']['actor'],'foreign')
            self.assertNotEqual(second_job['job']['job_id'],saved['job']['job_id']);self.assertEqual(first_bytes,file_digest(first_path))
            self.assertEqual(owner.get(f.base_url+operator.PATH+'?request_id='+command['request_id']).json()['job']['job_id'],saved['job']['job_id'])
            self.assertEqual(foreign.get(f.base_url+operator.PATH+'?request_id='+command['request_id']).json()['job']['job_id'],second_job['job']['job_id'])
            self.assertEqual(foreign.post(f.base_url+path,data=dict(command,prices=[800])).status,409)
            self.assertEqual(first_bytes,file_digest(first_path))
            for api in (owner,foreign,denied):api.dispose()

    def test_chromium_click_time_operands_before_web_lock_and_buyer_preflight(self):
        with fixture() as f,sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':940});_open_manual_panel(page,f.base_url)
            page.locator('[data-spp-test-price-index="0"]').fill('810');page.wait_for_function('() => document.querySelector("[data-spp-test-start]").disabled === false')
            operator_scope=page.request.get(f.base_url+operator.PATH).json()['operator_scope']
            key='operator-spp-start:'+operator_scope+':/v1/sheet-vitrina-v1/prices/spp-test/start'
            page.evaluate("key=>{navigator.locks.request(key,()=>new Promise(resolve=>{window.releaseSppGate=resolve;}));}",key)
            page.wait_for_function('()=>!!window.releaseSppGate')
            posts=[];preflight_records=[]
            def intercept(route):
                request=route.request
                if request.url.endswith('/buyer-session/check'):
                    preflight_records.append(page.evaluate('key=>JSON.parse(localStorage.getItem(key))',key))
                    page.locator('[data-spp-test-price-index="0"]').fill('700')
                if request.method=='POST' and request.url.endswith('/spp-test/start'):posts.append(request.post_data_json)
                route.continue_()
            page.route('**/*',intercept)
            page.locator('[data-spp-test-start]').evaluate('(node)=>node.click()')
            page.locator('[data-spp-test-price-index="0"]').fill('800')
            page.evaluate('()=>window.releaseSppGate()')
            page.wait_for_function('()=>document.querySelector("[data-spp-test-acceptance]").innerText.includes("Задание сохранено")')
            self.assertEqual(len(posts),1);self.assertEqual(posts[0]['prices'],[810]);self.assertEqual(preflight_records[0]['body']['prices'],[810])
            self.assertEqual(page.locator('[data-spp-test-price-index="0"]').input_value(),'700')
            browser.close()

    def test_chromium_flags_only_badges_history_and_lost_reply(self):
        with fixture() as f,sync_playwright() as pw:
            browser=pw.chromium.launch();context=browser.new_context(viewport={'width':1440,'height':940});page=context.new_page();posts=[];saved=[]
            def intercept(route):
                if route.request.method=='POST' and route.request.url.endswith('/spp-test/start'):
                    posts.append(route.request.post_data_json);reply=route.fetch();self.assertEqual(reply.status,200);saved.append(reply.json());route.abort()
                else:route.continue_()
            metadata=_spp_tab_metadata(page)
            context.route('**/*',intercept);_open_manual_panel(page,f.base_url)
            page.locator('[data-spp-test-price-index="0"]').fill('810')
            try:
                page.wait_for_function('()=>document.querySelector("[data-spp-test-start]").disabled===false')
            except PlaywrightTimeoutError:
                _spp_start_timeout_diagnostic(page, 'flags', metadata)
                raise
            page.locator('[data-spp-test-start]').click()
            page.wait_for_function('()=>document.querySelector("[data-spp-test-acceptance]").innerText.includes("Задание сохранено")')
            page.wait_for_function('()=>document.querySelector("[data-spp-test-state]").innerText.includes("готово")')
            self.assertEqual(len(posts),1)
            import sqlite3
            conn=sqlite3.connect(f.runtime.db_path)
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(ATTEMPT_EVENTS_TABLE,)).fetchall():conn.execute('DROP TRIGGER '+row[0])
            conn.execute(f'DELETE FROM {ATTEMPT_EVENTS_TABLE}');conn.commit();conn.close()
            page.reload();page.locator('[data-unified-tab-button="sku-management"]').click();page.locator('[data-sku-management-subtab="prices"]').click();page.locator('[data-prices-subtab="spp-test"]').click()
            page.wait_for_function('()=>document.querySelector("[data-spp-test-state]").innerText.includes("Результат требует внимания")')
            self.assertIn('Ожидает подтверждения WB',page.locator('[data-spp-test-measurements]').inner_text())
            self.assertIn('Результат требует внимания',page.locator('[data-spp-history-list]').inner_text())
            self.assertEqual(len(posts),1);self.assertTrue(page.locator('[data-spp-test-start]').is_disabled())
            context.close();browser.close()


if __name__=='__main__':unittest.main(verbosity=2)
