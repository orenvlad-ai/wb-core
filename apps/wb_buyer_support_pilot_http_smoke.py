#!/usr/bin/env python3
"""Synthetic HTTP and browser checks; never contacts a paid provider or WB."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib import error, request
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

class HttpTest(unittest.TestCase):
    def test_session_csrf_cabinet_confirmation_and_safe_errors(self):
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        from packages.application.wb_buyer_support_pilot import BuyerSupportPilotError
        with TemporaryDirectory() as tmp:
            runtime=Path(tmp); pilot=Mock(); pilot.send.return_value={'operation_id':'o1','state':'unknown'}
            pilot.operation.return_value={'operation_id':'o1','state':'unknown'}
            app=RegistryUploadHttpEntrypoint(runtime,buyer_support_pilot=pilot)
            config=RegistryUploadHttpEntrypointConfig('127.0.0.1',0,'/bundle','/plan','/refresh','/status','/operator',runtime)
            def user(h,c):
                role=h.headers.get('X-Test-Role')
                return None if not role else {'username':'session-user','role':'operator','allowed_sections':[role]}
            with patch.dict(os.environ,{'WB_BUYER_SUPPORT_CABINET_ID':'A'}), patch('packages.adapters.registry_upload_http_entrypoint._web_auth_config',return_value={'enabled':True,'configured':True}),patch('packages.adapters.registry_upload_http_entrypoint._authenticated_web_user',side_effect=user),patch('packages.adapters.registry_upload_http_entrypoint._ensure_business_data_write_allowed',return_value=True):
                server=build_registry_upload_http_server(config,entrypoint=app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base='http://127.0.0.1:'+str(server.server_address[1])+'/v1/sheet-vitrina-v1/feedbacks/buyer-support/pilot'
                def call(path,body=None,role='feedbacks',extra=None):
                    headers={'Accept':'application/json','Content-Type':'application/json','X-WB-Buyer-Support-CSRF':'1'}
                    if role: headers['X-Test-Role']=role
                    headers.update(extra or {})
                    req=request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
                    try:
                        with request.urlopen(req) as r:return r.status,json.loads(r.read())
                    except error.HTTPError as e:return e.code,json.loads(e.read())
                body={'draft_id':'d1','expected_context_version':'v1','request_id':'r1','confirmed':True}
                try:
                    self.assertEqual(call('/send',body,role=None)[0],401)
                    self.assertEqual(call('/send',body,role='prices')[0],403)
                    self.assertEqual(call('/send',body,extra={'Origin':'https://other.test'})[0],403)
                    self.assertEqual(call('/send',body,extra={'X-WB-Buyer-Support-CSRF':''})[0],403)
                    self.assertEqual(call('/send',body,extra={'Sec-Fetch-Site':'same-site'})[0],403)
                    for confirmed in (False,'true',1,None):
                        self.assertEqual(call('/send',{**body,'confirmed':confirmed})[0],422)
                    for field in ('actor','cabinet','text'):
                        self.assertEqual(call('/send',{**body,field:'B'})[0],422)
                    self.assertEqual(call('/send?cabinet=B',body)[0],422)
                    with patch('packages.adapters.registry_upload_http_entrypoint._web_auth_config',return_value={'enabled':False,'configured':True}):
                        self.assertEqual(call('/send',body)[0],401)
                        self.assertEqual(call('/propose',{'kind':'chat','item_id':'c1','request_id':'r-disabled','expected_context_version':'v1'})[0],401)
                        self.assertEqual(call('/operation?request_id=r1')[0],401)
                    pilot.send.assert_not_called()
                    self.assertEqual(call('/send',body)[0],200)
                    pilot.send.assert_called_once_with('A',**body,actor='session-user')
                    self.assertEqual(call('/operation?request_id=r1')[0],200)
                    pilot.operation.assert_called_once_with('A',operation_id='',request_id='r1')
                    self.assertEqual(call('/operation?request_id=r1&operation_id=o1')[0],422)
                    claim_body={**body,'claim_id':'claim1','action':'approve2','expected_claim_version':'cv1','text_basis_confirmed':True}
                    pilot.claim.return_value={'operation_id':'o2','state':'pending_readback'}
                    self.assertEqual(call('/claim',{**claim_body,'confirmed':False})[0],422)
                    for attestation in (False,'true',1,None):
                        self.assertEqual(call('/claim',{**claim_body,'text_basis_confirmed':attestation})[0],422)
                    pilot.claim.assert_not_called()
                    self.assertEqual(call('/claim',claim_body)[0],200)
                    pilot.claim.assert_called_once_with('A',**claim_body,actor='session-user')
                    pilot.refresh.return_value={'context_version':'v2','messages':[],'claims':[]}
                    refresh={'kind':'chat','item_id':'chat1','request_id':'r-refresh'}
                    self.assertEqual(call('/refresh',refresh)[0],200)
                    pilot.refresh.assert_called_once_with('A',**refresh,actor='session-user')
                    pilot.propose.return_value={'draft_id':'d1','status':'ready'}
                    proposal={'kind':'chat','item_id':'chat1','expected_context_version':'v1','request_id':'r2'}
                    self.assertEqual(call('/propose',proposal)[0],200)
                    pilot.propose.assert_called_once_with('A',**proposal,actor='session-user')
                    pilot.reconcile.return_value={'operation_id':'o2','state':'confirmed'}
                    reconcile={'operation_id':'o2','request_id':'r3'}
                    self.assertEqual(call('/reconcile',reconcile)[0],200)
                    pilot.reconcile.assert_called_once_with('A',**reconcile,actor='session-user')
                    self.assertEqual(call('/send',{**body,'unexpected':'x'*9000})[0],413)
                    pilot.send.side_effect=BuyerSupportPilotError('stale_context',409)
                    self.assertEqual(call('/send',body)[0],409)
                    pilot.send.side_effect=RuntimeError('PRIVATE-TOKEN-AND-UPSTREAM-BODY')
                    code,result=call('/send',body);self.assertEqual(code,503);self.assertNotIn('PRIVATE',json.dumps(result))
                finally:server.shutdown();thread.join(2);server.server_close()

class RealServiceHttpIntegrationTest(unittest.TestCase):
    def test_actual_handler_service_lifecycle_and_lost_terminal_failures(self):
        from http.client import RemoteDisconnected
        from apps.wb_buyer_support_pilot_smoke import CABINET, CHAT, CLAIM, FakeProvider, FakeWb, append, claim_row
        from packages.application.wb_buyer_support import BuyerSupportRepository
        from packages.application.wb_buyer_support_pilot import BuyerSupportPilotService, PilotConfig
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        import packages.adapters.registry_upload_http_entrypoint as http
        with TemporaryDirectory() as tmp:
            for flow in ('send', 'claim'):
                with self.subTest(flow=flow):
                    runtime=Path(tmp)/flow
                    repo=BuyerSupportRepository(runtime)
                    repo.save_chats(CABINET,{'result':[{'chatID':CHAT,'clientName':'Owner','goodCard':{'rid':'purchase','nmID':123,'name':'Glass'}}]})
                    append(repo)
                    repo.save_claim_page(CABINET,{'claims':[claim_row()],'total':1},archive=False)
                    provider,wb=FakeProvider(),FakeWb()
                    if flow=='claim':provider.action='autorefund1'
                    pilot=BuyerSupportPilotService(runtime,repo,provider,wb,PilotConfig(True,CABINET,frozenset([CHAT]),frozenset([CLAIM]),'personal-service'))
                    app=RegistryUploadHttpEntrypoint(runtime,buyer_support_pilot=pilot)
                    config=RegistryUploadHttpEntrypointConfig('127.0.0.1',0,'/bundle','/plan','/refresh','/status','/operator',runtime)
                    user={'username':'integration-operator','role':'operator','allowed_sections':['feedbacks']}
                    with patch.dict(os.environ,{'WB_BUYER_SUPPORT_CABINET_ID':CABINET}),patch.object(http,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(http,'_authenticated_web_user',return_value=user),patch.object(http,'_ensure_business_data_write_allowed',return_value=True):
                        server=http.build_registry_upload_http_server(config,entrypoint=app)
                        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                        base='http://127.0.0.1:'+str(server.server_address[1])+'/v1/sheet-vitrina-v1/feedbacks/buyer-support'
                        responses=[]
                        def call(path,body=None):
                            req=request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Accept':'application/json','Content-Type':'application/json','X-WB-Buyer-Support-CSRF':'1'})
                            try:
                                with request.urlopen(req,timeout=5) as response:result=(response.status,json.loads(response.read()))
                            except error.HTTPError as exc:result=(exc.code,json.loads(exc.read()))
                            responses.append(result[1]);return result
                        try:
                            code,initial=call('/detail?kind=chat&id='+CHAT);self.assertEqual(code,200)
                            self.assertFalse(initial['pilot']['source_fresh'])
                            provider.configured=False
                            gate_request={'kind':'chat','item_id':CHAT,'expected_context_version':initial['context_version'],'request_id':flow+'-provider-off'}
                            code,rejected=call('/pilot/propose',gate_request)
                            self.assertEqual(code,503);self.assertIs(rejected['not_accepted'],True);self.assertIs(rejected['write_attempted'],False)
                            self.assertEqual(rejected['request_id'],gate_request['request_id'])
                            self.assertEqual(call('/pilot/operation?request_id='+gate_request['request_id'])[0],404)
                            self.assertEqual(provider.calls,0);provider.configured=True

                            no_source={'kind':'chat','item_id':CHAT,'expected_context_version':initial['context_version'],'request_id':flow+'-no-source'}
                            self.assertEqual(call('/pilot/propose',no_source)[0],409)
                            code,failed=call('/pilot/operation?request_id='+no_source['request_id'])
                            self.assertEqual(code,200);self.assertEqual((failed['kind'],failed['state'],failed['write_attempted']),('propose','failed',False))
                            self.assertEqual((provider.calls,wb.sends,wb.decisions),(0,0,0))
                            code,fresh=call('/pilot/refresh',{'kind':'chat','item_id':CHAT,'request_id':flow+'-refresh'})
                            self.assertEqual(code,200);self.assertTrue(fresh['pilot']['source_fresh'])
                            reads=wb.reads
                            code,refresh_receipt=call('/pilot/operation?request_id='+flow+'-refresh')
                            self.assertEqual(code,200);self.assertEqual(refresh_receipt['context_version'],fresh['context_version']);self.assertEqual(wb.reads,reads)
                            code,draft=call('/pilot/propose',{'kind':'chat','item_id':CHAT,'expected_context_version':fresh['context_version'],'request_id':flow+'-propose'})
                            self.assertEqual(code,200);self.assertEqual(draft['status'],'ready');self.assertEqual(provider.calls,1)
                            command={'draft_id':draft['draft_id'],'expected_context_version':draft['context_version'],'request_id':flow+'-write','confirmed':True}
                            if flow=='claim':
                                proposal=draft['return_proposal'];self.assertEqual(proposal['basis_type'],'text_sufficient')
                                command.update(claim_id=proposal['claim_id'],action=proposal['action'],expected_claim_version=proposal['claim_version'],text_basis_confirmed=True)
                            bad={**command,'request_id':flow+'-lost-failure'}
                            if flow=='send':bad['expected_context_version']='stale-version'
                            else:bad['expected_claim_version']='stale-claim-version'
                            original_write=http._write_json_response
                            dropped=[]
                            def lose_failure(handler,status,payload,**kwargs):
                                if handler.path.endswith('/pilot/'+flow) and int(status)==409 and not dropped:
                                    dropped.append(True);handler.close_connection=True;return
                                return original_write(handler,status,payload,**kwargs)
                            with patch.object(http,'_write_json_response',side_effect=lose_failure):
                                with self.assertRaises((RemoteDisconnected,error.URLError,ConnectionError)):
                                    call('/pilot/'+flow,bad)
                            self.assertEqual(dropped,[True])
                            self.assertEqual((wb.sends,wb.decisions),(0,0))
                            code,receipt=call('/pilot/operation?request_id='+bad['request_id'])
                            self.assertEqual(code,200);self.assertEqual(receipt['request_id'],bad['request_id']);self.assertEqual(receipt['state'],'failed');self.assertIs(receipt['write_attempted'],False)
                            self.assertEqual(receipt['kind'],'chat_send' if flow=='send' else 'claim_decision')
                            code,operation=call('/pilot/'+flow,command)
                            self.assertEqual(code,200);self.assertEqual(operation['state'],'pending_readback')
                            self.assertEqual((wb.sends,wb.decisions),(1,0) if flow=='send' else (0,1))
                            code,read_operation=call('/pilot/operation?request_id='+command['request_id'])
                            self.assertEqual(code,200);self.assertEqual(read_operation['operation_id'],operation['operation_id'])
                            code,confirmed=call('/pilot/reconcile',{'operation_id':operation['operation_id'],'request_id':flow+'-readback'})
                            self.assertEqual(code,200);self.assertEqual(confirmed['state'],'confirmed')
                            code,detail=call('/detail?kind=chat&id='+CHAT);self.assertEqual(code,200)
                            if flow=='send':
                                self.assertEqual(detail['messages'][-1]['sender'],'seller');self.assertEqual(provider.calls,1)
                            else:
                                self.assertTrue(confirmed['refresh_draft_id']);self.assertEqual(detail['workflow']['latest_draft']['status'],'ready')
                                self.assertEqual(detail['workflow']['latest_draft']['draft_id'],confirmed['refresh_draft_id'])
                                self.assertIsNone(detail['workflow']['latest_draft']['return_proposal']);self.assertEqual(provider.calls,2)
                                self.assertEqual(wb.sends,0);self.assertTrue(detail['workflow']['latest_draft']['text'])
                            self.assertEqual((wb.sends,wb.decisions),(1,0) if flow=='send' else (0,1))
                            serialized=json.dumps(responses)
                            self.assertNotIn('transient-not-stored',serialized);self.assertNotIn('replySign',serialized)
                        finally:server.shutdown();thread.join(2);server.server_close()


class RendererTest(unittest.TestCase):
    def test_pilot_disabled_allowlist_stale_xss_and_separate_return(self):
        import subprocess
        code='''const assert=require('assert'),r=require(process.argv[1]);
const item={context_version:'v1',pilot:{enabled:true,allowlisted:true,provider_configured:true,wb_configured:true,source_fresh:true},workflow:{latest_draft:{draft_id:'d1',status:'ready',context_version:'v1',text:'<img src=x>',return_proposal:null}}};
assert(r.renderPilot({}).includes('пилот отключён'));
assert(!r.renderPilot({...item,pilot:{...item.pilot,allowlisted:false}}).includes('data-bs-action'));
assert(r.renderPilot(item).includes('data-bs-action="send"'));assert(!r.renderPilot(item).includes('<img'));
assert(!r.renderPilot({...item,context_version:'v2'}).includes('data-bs-action="send"'));
item.workflow.operations=[{operation_id:'o1',state:'unknown',kind:'chat_send',can_reconcile:true}];
for (const pilot of [{...item.pilot,enabled:false},{...item.pilot,provider_configured:false},{...item.pilot,allowlisted:false}]) {
 let html=r.renderPilot({...item,pilot});assert(html.includes('data-bs-action="reconcile"'));assert(html.includes('data-bs-recovery'));assert(html.includes('o1'));
}
let pendingReply=r.renderPilot({...item,pilot:{...item.pilot,provider_configured:false},workflow:{operations:[{operation_id:'o-return',kind:'claim_decision',state:'confirmed',can_reconcile:true,response_draft_state:'draft_pending',draft_error:'provider_not_configured'}]}});
assert(pendingReply.includes('Продолжить подготовку ответа'));assert(pendingReply.includes('ещё не подготовлен'));
item.workflow.latest_draft.return_proposal={claim_id:'c1',action:'approve2',basis_type:'text_sufficient',claim_version:'cv1',basis:'<script>x</script>'};
let html=r.renderPilot(item);assert(html.includes('Со сдачей товара'));assert(html.includes('data-bs-action="claim"'));assert(html.includes('data-bs-action="send"'));assert(html.includes('data-bs-return-confirm'));assert(!html.includes('<script>'));
item.workflow.latest_draft.return_proposal.basis_type='media_required';html=r.renderPilot(item);assert(!html.includes('data-bs-action="claim"'));assert(html.includes('Просмотр фото/видео в пилоте недоступен'));
'''
        subprocess.run(['node','-e',code,str(ROOT/'packages/adapters/templates/wb_buyer_support.js')],check=True)

if __name__=='__main__':unittest.main()
