#!/usr/bin/env python3
"""Native disposable cleaner command/result proofs and actual HTTP/UI.

All external fixtures are loopback FakeWB. No provider credentials or live work.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import json,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application import operator_cleaner_operations as source,operator_operations as journal
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account,Principal

OWNER=Principal('owner',True,True,True)


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name);self.registry=StoreRegistry(self.runtime)
        self.cleaner=KeywordCleaner(CleanerStore(self.registry),Account('fixture-seller','fixture-account'),owner_username='owner')
        self.cleaner.initialize(generation='fixture-generation');self.web=CleanerWeb(self.cleaner,generation='fixture-generation')
        self.scope=source.CleanerScope(self.runtime,self.cleaner.key,'fixture-seller','fixture-account','fixture-generation','owner')
    def save(self,identity='fixture-settings-001',revision=1,principal=OWNER,time='07:00'):
        return self.cleaner.update_settings(dict(request_id=identity,expected_revision=revision,enabled=False,schedule_time=time),principal)
    def listing(self,scope=None,allowed=None,**kwargs):
        return journal.journal(self.registry.resolve('operational'),allowed_domains={source.DOMAIN} if allowed is None else allowed,
            cleaner_scope=self.scope if scope is None else scope,**kwargs)
    def test_native_settings_same_ID_ABA_and_immutable_proof_source_only(self):
        self.save();first=source.receipt(self.web,OWNER,'fixture-settings-001')
        self.save('fixture-settings-002',2,time='08:00');self.save('fixture-settings-003',3)
        self.assertEqual(self.save()['settings_revision'],2)
        self.assertEqual(source.receipt(self.web,OWNER,'fixture-settings-001'),first)
        self.assertEqual(first['source_ref']['settings_revision'],2);self.assertEqual(first['primary_effect'],'source_saved')
        self.assertFalse(first['external_confirmed']);self.assertFalse(first['calculation_completed'])
        self.assertEqual(self.listing()['total'],3)
    def test_scope_filters_before_counts_search_pages_details_and_query_only(self):
        self.save();foreign=Principal('foreign',True,True,True,True)
        self.save('fixture-foreign-001',2,foreign)
        db=self.registry.resolve('operational');before=db.read_bytes()
        self.assertEqual(self.listing()['total'],1);self.assertEqual(self.listing(search='foreign')['total'],0)
        identity='cleaner-request:fixture-settings-001'
        for scope in (replace(self.scope,actor='unknown'),replace(self.scope,generation='other')):
            self.assertEqual(self.listing(scope)['total'],0)
            self.assertIsNone(journal.read_acceptance(db,identity,allowed_domains={source.DOMAIN},cleaner_scope=scope))
        self.assertEqual(self.listing(allowed=set())['total'],0);self.assertEqual(self.listing(page=100)['items'],[])
        with patch.object(CleanerStore,'initialize',side_effect=AssertionError('GET bootstrap forbidden')):
            self.assertEqual(self.listing()['total'],1)
        self.assertEqual(db.read_bytes(),before)
    def test_actual_native_write_readback_not_prepared_or_partial_completion(self):
        from apps.search_cluster_cleaner_web_fixture import running_fixture
        with running_fixture('confirmed') as f:
            scope=source.CleanerScope.from_entrypoint(f.entrypoint,actor='owner')
            db=f.cleaner.store.registry.resolve('operational')
            receipt=journal.read_acceptance(db,'cleaner-request:fixture-D-manual',allowed_domains={source.DOMAIN},cleaner_scope=scope)
            self.assertEqual(receipt['state'],'completed');self.assertTrue(receipt['external_confirmed']);self.assertGreater(receipt['expected_write_count'],0)
            # Synthetic adversarial counterexample: source expected target set
            # must not be covered by an unrelated confirmed operation set.
            with f.cleaner.store.transaction() as c:
                run=c.execute("SELECT outcome FROM cleaner_requests WHERE request_id='fixture-D-manual'").fetchone()
                run_id=json.loads(run[0])['run_id']
                c.execute('UPDATE cleaner_runs SET targets=? WHERE run_id=?',(json.dumps([dict(target='999:999',query_hash='foreign',decision_id='foreign')]),run_id))
            blocked=journal.read_acceptance(db,'cleaner-request:fixture-D-manual',allowed_domains={source.DOMAIN},cleaner_scope=scope)
            self.assertFalse(blocked['external_confirmed']);self.assertNotEqual(blocked['state'],'completed')
    def test_native_frozen_group_missing_child_cannot_complete_and_foreign_HTTP_invisible(self):
        from apps.search_cluster_cleaner_web_fixture import running_fixture
        with running_fixture() as f:
            f.cleaner.update_settings(dict(request_id='fixture-disable-batch',expected_revision=2,enabled=False),OWNER)
            snapshot=f.web.batch_eligibility(OWNER)['items']
            selected=next(item for item in snapshot if item['eligible'])
            command=dict(request_id='fixture-new-batch',selected_categories=['active'],targets=[dict(advert_id=selected['advert_id'],nm_id=selected['nm_id'])])
            f.cleaner.start_manual_batch(command,OWNER,snapshot=snapshot)
            receipt=source.receipt(f.web,OWNER,command['request_id'])
            self.assertEqual(receipt['state'],'processing');self.assertFalse(receipt['external_confirmed']);self.assertEqual(len(receipt['children']),1)
            # A native terminal label without its exact saved child/results does
            # not manufacture completion, even though the request is durable.
            f.cleaner.record_manual_batch(command['request_id'],state='complete',stage='finished',current_index=1)
            terminal=source.receipt(f.web,OWNER,command['request_id'])
            self.assertEqual(terminal['state'],'needs_attention');self.assertFalse(terminal['external_confirmed'])
            for actor in ('reader','admin','noads'):
                opener=f.login(actor)
                from urllib.error import HTTPError
                def get(path):
                    try:response=opener.open(f.base_url+path)
                    except HTTPError as exc:response=exc
                    with response:return response.status,json.load(response)
                status,payload=get('/v1/sheet-vitrina-v1/operations?domain=cleaner_operations&search=fixture-new-batch')
                self.assertEqual(status,200,(actor,payload));self.assertEqual(payload['total'],0)
                status,_=get('/v1/sheet-vitrina-v1/operations/cleaner-request:fixture-new-batch')
                self.assertEqual(status,404)
    def test_actual_native_UI_lost_POST_reload_exact_GET_only(self):
        from apps.search_cluster_cleaner_web_fixture import running_fixture,PASSWORD
        from playwright.sync_api import sync_playwright
        with running_fixture() as f,sync_playwright() as pw:
            browser=pw.chromium.launch();context=browser.new_context()
            try:
                page=context.new_page();posts=[];reads=[];lost={'value':True}
                page.goto(f.base_url+'/login');page.locator('input[name="username"]').fill('owner');page.locator('input[name="password"]').fill(PASSWORD);page.locator('button[type="submit"]').click()
                page.goto(f.url);page.wait_for_function('kc.summary && !kc.stale')
                def commands(route):
                    if route.request.method!='POST':route.continue_();return
                    posts.append(route.request.post_data_json);response=route.fetch();self.assertEqual(response.status,202,response.text());route.abort('failed')
                def recover(route):
                    reads.append(route.request.url)
                    if lost['value']:route.abort('failed')
                    else:route.continue_()
                page.route('**/keyword-cleaner/settings',commands);page.route('**/keyword-cleaner/requests/*',recover)
                page.evaluate("async()=>{await kcCommand('/settings',{expected_revision:kc.summary.settings.revision,enabled:false});}")
                self.assertEqual(len(posts),1);identity=posts[0]['request_id'];self.assertTrue(page.evaluate('!!kc.pending'))
                lost['value']=False;page.reload();page.wait_for_selector('[data-kc-operation-receipt] [data-ff-operation-receipt]')
                self.assertEqual(page.locator('[data-kc-operation-receipt] [data-ff-operation-receipt]').get_attribute('data-ff-operation-receipt'),'cleaner-request:'+identity)
                self.assertEqual(page.locator('[data-kc-operation-receipt] .ff-operation-status').inner_text(),'Сохранено')
                self.assertEqual(len(posts),1);self.assertGreaterEqual(len(reads),2)
                self.assertTrue(all(url.endswith(identity) for url in reads))
                # Native source is disabled immediately, independent of receipt.
                self.assertFalse(f.cleaner.summary(OWNER)['settings']['enabled'])
            finally:context.close();browser.close()


if __name__=='__main__':unittest.main()
