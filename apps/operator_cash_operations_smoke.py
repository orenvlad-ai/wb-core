#!/usr/bin/env python3
"""Actual isolated native cash receipts; API fixture is read-only, no live money."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import json,sys,unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application.finance_liquidity_cash import FinanceCashService,FinanceCashError,bootstrap_finance_cash_store
from packages.application.operator_cash_operations import read
from packages.adapters.finance_liquidity_auth import FixtureFinanceAuth
from packages.adapters.finance_liquidity_http import FinanceHttpApp,build_finance_http_server

class CashProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=self.root/'finance.sqlite3'
        bootstrap_finance_cash_store(self.db);self.service=FinanceCashService(self.db)
        payload=dict(document_type='opening',target_account_id='cash_vladislav',amount='123.45',occurred_at='2026-07-01T00:00:00Z',opening_evidence_type='manual_confirmation',opening_evidence_ref='Synthetic opening')
        self.draft=self.service.create_document(payload,'one','one-draft','one-draft-key')
        self.post=self.service.post_document(self.draft['document_id'],dict(base_revision=1),'one','one-post','one-post-key')
        self.foreign=self.service.create_document(dict(payload,target_account_id='cash_victoria'),'two','two-draft','two-draft-key')
        self.reconcile=self.service.record_reconciliation(dict(account_id='cash_vladislav',week_ending='2026-07-20',actual_amount='100.00',comment='Synthetic discrepancy'),'one','one-rec','one-rec-key')
    def read(self,**kwargs):
        return read(self.service,actor='one',is_admin=False,store_id='fixture-cash',store_mode='isolated_test',**kwargs)
    def test_native_receipts_draft_ledger_and_protected_reconciliation_no_changes(self):
        before=self.db.read_bytes()
        receipt=self.read(identity='one-draft')['operation'];self.assertEqual(receipt['primary_effect'],'draft')
        self.assertEqual(receipt['source_ref']['receipt_id'],self.draft['receipt_id'])
        posted=self.read(identity='one-post')['operation'];self.assertEqual(posted['primary_effect'],'cash_ledger');self.assertEqual(posted['state'],'completed')
        hidden=self.read(identity='one-rec')['operation'];self.assertEqual(hidden['native_state'],'hidden')
        self.assertNotIn('discrepancy',json.dumps(hidden));self.assertNotIn('100.00',json.dumps(hidden))
        self.assertNotIn('balance',json.dumps(hidden));self.assertNotIn('12345',json.dumps(hidden))
        visible=self.read(identity='one-rec',permitted_balance=True)['operation'];self.assertEqual(visible['native_state'],'discrepancy')
        self.assertEqual(self.db.read_bytes(),before)
        with patch.object(self.service,'_assert_ledger_integrity',side_effect=FinanceCashError('ledger_integrity_unavailable','Synthetic failed native proof',503)):
            with self.assertRaisesRegex(FinanceCashError,'Synthetic failed'):self.read(identity='one-post')
    def test_actor_filter_before_count_search_page_detail_and_unbound_TEST_no_reads(self):
        listing=self.read();self.assertEqual(listing['total'],3)
        self.assertEqual(self.read(search='two-draft')['total'],0)
        self.assertFalse(self.read(page=4,limit=1)['items'])
        with self.assertRaises(FinanceCashError) as error:self.read(identity='two-draft')
        self.assertEqual(error.exception.status,404)
        admin=read(self.service,actor='admin',is_admin=True,store_id='fixture-cash',store_mode='isolated_test')
        self.assertEqual(admin['total'],4)
        with patch.object(self.service,'_connect',side_effect=AssertionError('No reads before TEST admission')):
            with self.assertRaises(FinanceCashError):read(self.service,actor='one',is_admin=False,store_id='fixture-cash',store_mode='live')
    def test_actual_native_http_permissions_redaction_strict_queries_no_writers(self):
        auth=self.root/'auth.json';auth.write_text(json.dumps({'actors':{
            'one':{'username':'one','role':'operator','capabilities':['finance']},
            'denied':{'username':'denied','role':'operator','capabilities':[]},
            'admin':{'username':'admin','role':'operator','capabilities':['finance_admin']}}}))
        app=FinanceHttpApp(self.service,FixtureFinanceAuth(auth),read_enabled=True,write_enabled=False,csrf_secret='fixture',static_dir=ROOT/'packages/adapters/finance_liquidity_static',allowed_origin='',store_id='fixture-cash',store_mode='isolated_test')
        server=build_finance_http_server('127.0.0.1',0,app);t=Thread(target=server.serve_forever,daemon=True);t.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);before=self.db.read_bytes()
        def get(path,actor='one'):
            try:
                with urlopen(Request(base+'/v1/finance'+path,headers={'Cookie':'finance_fixture_session='+actor})) as r:return r.status,json.loads(r.read())
            except HTTPError as e:return e.code,json.loads(e.read())
        try:
            self.assertEqual(get('/operator-operations?search=two-draft')[1]['data']['total'],0)
            self.assertEqual(get('/operator-operations?search=one',actor='denied')[0],403)
            self.assertEqual(get('/operator-operations/two-draft')[0],404)
            self.assertEqual(get('/operator-operations/two-draft','admin')[0],200)
            self.assertEqual(get('/operator-operations/one-rec')[1]['data']['operation']['native_state'],'hidden')
            for path in ('/operator-operations?actor=two','/operator-operations?page=1&page=2','/operator-operations/one-post?actor=admin'):
                self.assertEqual(get(path)[0],422)
            self.assertEqual(self.db.read_bytes(),before)
        finally:server.shutdown();server.server_close();t.join(5)

    def test_actual_TEST_finance_journal_browser_readonly_drafts_and_hidden_balance(self):
        from playwright.sync_api import sync_playwright
        auth=self.root/'auth.json';auth.write_text(json.dumps({'actors':{'one':{'username':'one','role':'operator','capabilities':['finance']}}}))
        app=FinanceHttpApp(self.service,FixtureFinanceAuth(auth),read_enabled=True,write_enabled=False,csrf_secret='fixture',static_dir=ROOT/'packages/adapters/finance_liquidity_static',allowed_origin='',store_id='fixture-cash',store_mode='isolated_test',instance_label='TEST native fixture')
        server=build_finance_http_server('127.0.0.1',0,app);t=Thread(target=server.serve_forever,daemon=True);t.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);before=self.db.read_bytes()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch()
                try:
                    ctx=browser.new_context();ctx.add_cookies([dict(name='finance_fixture_session',value='one',url=base)])
                    page=ctx.new_page();calls=[];errors=[]
                    page.on('request',lambda r:calls.append((r.method,r.url)))
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(base+'/finance/?embedded=1')
                    page.locator('#operator-journal summary').click()
                    page.locator('[data-cash-operator-total]').filter(has_text='Операций: 3').wait_for(state='attached')
                    self.assertEqual(page.locator('[data-cash-operator-rows] .ff-operation-status').count(),3)
                    self.assertEqual(page.locator('[data-cash-operator-rows]').get_by_text('Черновик сохранён',exact=True).count(),1)
                    self.assertIn('Результат сверки скрыт',page.locator('[data-cash-operator-rows]').inner_text())
                    self.assertNotIn('two-draft',page.locator('[data-cash-operator-rows]').inner_text())
                    protected=page.locator('[data-account-id="cash_vladislav"] .balance');self.assertEqual(protected.inner_text(),'—')
                    page.locator('[data-cash-operator-search] input').fill('two-draft');page.locator('[data-cash-operator-search] button').click()
                    page.locator('[data-cash-operator-total]').filter(has_text='Операций: 0').wait_for()
                    self.assertFalse(page.locator('[data-cash-operator-rows]').inner_text())
                    self.assertTrue(all(method=='GET' for method,_ in calls),calls)
                    self.assertFalse(errors,errors)
                finally:browser.close()
            self.assertEqual(self.db.read_bytes(),before)
        finally:server.shutdown();server.server_close();t.join(5)

    def test_actual_native_async_TEST_scope_refresh_fences_late_receipt_and_journal(self):
        from playwright.sync_api import sync_playwright
        auth=self.root/'auth.json';app=None
        def grants(enabled=True):
            auth.write_text(json.dumps({'actors':{name:{'username':name,'role':'operator','capabilities':['finance'] if enabled else []} for name in ('one','two')}}))
            if app is not None:app.auth=FixtureFinanceAuth(auth)
        grants()
        app=FinanceHttpApp(self.service,FixtureFinanceAuth(auth),read_enabled=True,write_enabled=False,csrf_secret='fixture',static_dir=ROOT/'packages/adapters/finance_liquidity_static',allowed_origin='',store_id='fixture-cash',store_mode='isolated_test')
        server=build_finance_http_server('127.0.0.1',0,app);t=Thread(target=server.serve_forever,daemon=True);t.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);before=self.db.read_bytes()
        original=(app.static_dir/'finance.js').read_text();marker='  if (!app) return; loadAll();'
        self.assertEqual(original.count(marker),1)
        # Expose private seams only in this served synthetic fixture; production
        # functions/requests stay unchanged and all proof payloads come from HTTP.
        script=original.replace(marker,'  window.cashScopeTest={showCashNativeReceipt,loadCashOperatorJournal,loadAll};\n'+marker)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch()
                try:
                    for change in ('same','mode','store','actor','grants','disabled'):
                        with self.subTest(change=change):
                            app.store_mode='isolated_test';app.store_id='fixture-cash';app.read_enabled=True;grants()
                            ctx=browser.new_context();ctx.add_cookies([dict(name='finance_fixture_session',value='one',url=base)])
                            page=ctx.new_page();held=[];calls=[];errors=[];hold={'target':None}
                            page.on('request',lambda r:calls.append((r.method,r.url)))
                            page.on('pageerror',lambda e:errors.append(str(e)))
                            def intercept(route):
                                path=route.request.url.split(base,1)[1]
                                if path=='/finance/finance.js':route.fulfill(content_type='text/javascript',body=script);return
                                if hold['target'] and path.startswith(hold['target']):
                                    hold['target']=None
                                    response=route.fetch();held.append((route,response));return
                                route.continue_()
                            def wait_held(count):
                                for _ in range(100):
                                    if len(held)>=count:break
                                    page.wait_for_timeout(10)
                                self.assertEqual(len(held),count)
                            page.route(base+'/**',intercept)
                            page.goto(base+'/finance/?embedded=1');page.evaluate('cashScopeTest.loadAll()')
                            hold['target']='/v1/finance/operator-operations/one-post'
                            page.evaluate('()=>{window.pendingCash=cashScopeTest.showCashNativeReceipt("one-post");}')
                            page.wait_for_function('window.pendingCash !== undefined');wait_held(1)
                            self.assertEqual(held[0][1].status,200)
                            if change=='mode':app.store_mode='live'
                            if change=='store':app.store_id='other-TEST'
                            if change=='actor':ctx.add_cookies([dict(name='finance_fixture_session',value='two',url=base)])
                            if change=='grants':grants(False)
                            if change=='disabled':app.read_enabled=False
                            page.evaluate('cashScopeTest.loadAll()')
                            if change in ('mode','grants','disabled'):
                                self.assertTrue(page.locator('#operator-journal').evaluate('n=>n.classList.contains("is-hidden")'))
                            # Return to exactly the original scope before release:
                            # semantic equality must not revive a prior epoch.
                            if change!='same':
                                app.store_mode='isolated_test';app.store_id='fixture-cash';app.read_enabled=True;grants()
                                ctx.add_cookies([dict(name='finance_fixture_session',value='one',url=base)])
                                page.evaluate('cashScopeTest.loadAll()')
                            held[0][0].fulfill(response=held[0][1]);page.evaluate('pendingCash')
                            self.assertEqual(page.locator('[data-cash-operator-receipt] .ff-operation-check').count(),1 if change=='same' else 0)
                            # A fresh allowed native receipt still renders.
                            page.evaluate('cashScopeTest.showCashNativeReceipt("one-post")')
                            self.assertEqual(page.locator('[data-cash-operator-receipt] .ff-operation-check').count(),1)
                            # Hold an authentic native journal response; leave TEST,
                            # clear old views, return and load a fresh allowed view.
                            page.locator('dialog.ff-operation-popup').get_by_role('button',name='Закрыть').click()
                            page.locator('#operator-journal summary').click()
                            page.locator('[data-cash-operator-total]').filter(has_text='Операций: 3').wait_for()
                            hold['target']='/v1/finance/operator-operations?'
                            page.evaluate('()=>{window.pendingJournal=cashScopeTest.loadCashOperatorJournal();}')
                            wait_held(2)
                            app.store_mode='live';page.evaluate('cashScopeTest.loadAll()')
                            self.assertEqual(page.locator('[data-cash-operator-receipt]').inner_text(),'')
                            self.assertEqual(page.locator('[data-cash-operator-rows]').inner_text(),'')
                            held[1][0].fulfill(response=held[1][1]);page.evaluate('pendingJournal')
                            self.assertEqual(page.locator('[data-cash-operator-rows]').inner_text(),'')
                            self.assertEqual(page.locator('[data-cash-operator-total]').inner_text(),'')
                            app.store_mode='isolated_test';page.evaluate('cashScopeTest.loadAll()')
                            page.locator('[data-cash-operator-total]').filter(has_text='Операций: 3').wait_for()
                            hold['target']='/v1/finance/operator-operations?'
                            page.evaluate('()=>{window.pendingJournal=cashScopeTest.loadCashOperatorJournal();}')
                            wait_held(3)
                            app.store_mode='live';page.evaluate('cashScopeTest.loadAll()')
                            app.store_mode='isolated_test';page.evaluate('cashScopeTest.loadAll()')
                            page.locator('[data-cash-operator-total]').filter(has_text='Операций: 3').wait_for()
                            fresh=page.locator('[data-cash-operator-rows]').inner_text()
                            # Both stale success and stale native failure must leave
                            # the newer allowed rows/total unchanged.
                            held[2][0].fulfill(response=held[2][1]);page.evaluate('pendingJournal')
                            self.assertEqual(page.locator('[data-cash-operator-rows]').inner_text(),fresh)
                            app.read_enabled=False;hold['target']='/v1/finance/operator-operations?'
                            page.evaluate('()=>{window.failedJournal=cashScopeTest.loadCashOperatorJournal();}')
                            wait_held(4);self.assertEqual(held[3][1].status,503)
                            page.evaluate('cashScopeTest.loadAll()')
                            app.read_enabled=True;page.evaluate('cashScopeTest.loadAll()')
                            page.locator('[data-cash-operator-total]').filter(has_text='Операций: 3').wait_for()
                            held[3][0].fulfill(response=held[3][1]);page.evaluate('failedJournal')
                            self.assertEqual(page.locator('[data-cash-operator-rows]').inner_text(),fresh)
                            self.assertEqual(page.locator('[data-cash-operator-total]').inner_text(),'Операций: 3')
                            self.assertTrue(all(method=='GET' for method,_ in calls),calls);self.assertFalse(errors,errors)
                            ctx.close()
                finally:browser.close()
            self.assertEqual(self.db.read_bytes(),before)
        finally:server.shutdown();server.server_close();t.join(5)



if __name__=='__main__':unittest.main()
