#!/usr/bin/env python3
"""Real browser + authenticated local HTTP; synthetic data and transport faults."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from apps.search_cluster_cleaner_web_fixture import running_fixture, PASSWORD, OWNER
from packages.adapters.search_cluster_cleaner_http import PREFIX


def browser_login(page,f,username='owner'):
    response=page.request.post(f.base_url+'/login',form={'username':username,'password':PASSWORD,'next':f.url.split(f.base_url)[1]})
    assert response.status==200
    page.goto(f.url,wait_until='domcontentloaded')
    expect(page.locator('[data-keyword-cleaner]')).to_be_visible()
    expect(page.locator('[data-kc-status]')).not_to_have_text('Загружаем…')
    expect(page.locator('[data-kc-checked]')).not_to_have_text('—')


def run(output:Path):
    output.mkdir(parents=True,exist_ok=True);checks=[];screens=[]
    def check(name,condition=True):
        assert condition,name
        checks.append(name)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        with running_fixture('confirmed') as f:
            page=browser.new_page(viewport={'width':1440,'height':1080});browser_login(page,f)
            expect(page.locator('[data-kc-excluded]')).to_have_text('3')
            text=page.locator('[data-keyword-cleaner]').inner_text()
            check('D_connected_confirmed_manual_and_late_visible','По решениям владельца подтверждено исключений: 1' in text and 'по решениям владельца 1' in text)
            check('D_confirmed_candidates_not_pending','Это не подтверждённые исключения' not in text)
            page.locator('[data-kc-history-open]').click()
            expect(page.locator('[data-kc-history]')).to_contain_text('Позднее подтверждение WB')
            check('D_history_real_late_event')
            page.screenshot(path=str(output/'confirmed.png'),full_page=True);screens.append('confirmed.png');page.close()
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:
                c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page(viewport={'width':1440,'height':1080});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            browser_login(page,f)
            expect(page.locator('[data-kc-status]')).to_have_text('Выполнено')
            expect(page.locator('[data-ads-panel]')).to_be_hidden()
            check('deep_link_cleaner_inside_current_ads_navigation',page.locator('[data-sku-management-subtab="ads"]').get_attribute('aria-selected')=='true')
            check('xss_is_text',page.locator('[data-kc-reviews] img').count()==0 and page.evaluate('window.cleanerXss===undefined') and '<img' in page.locator('[data-kc-reviews]').inner_text())
            page.screenshot(path=str(output/'desktop.png'),full_page=True);screens.append('desktop.png')
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'mobile.png'),full_page=True);screens.append('mobile.png')
            check('mobile_cleaner_no_horizontal_overflow',page.locator('[data-keyword-cleaner]').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1'))
            page.set_viewport_size({'width':1440,'height':1080})
            # A manual operation requires a named campaign and its exact SKU.
            expect(page.locator('[data-kc-time]')).to_have_count(0)
            expect(page.locator('[data-keyword-cleaner]')).to_contain_text('Расписание выключено')
            expect(page.locator('[data-kc-enabled]')).to_have_count(0)
            before=f.count('cleaner_requests');expect(page.locator('[data-kc-run]')).to_be_disabled()
            expect(page.locator('[data-kc-manual-advert]')).to_contain_text('Тестовая кампания')
            check('manual_only_ui_has_no_schedule_or_implicit_run',f.count('cleaner_requests')==before)
            # A decision is saved once even when double-clicked, and becomes history.
            before=f.count('cleaner_manual_overrides');question=page.locator('[data-kc-review]').first
            question.get_by_role('button',name='Оставить',exact=True).dblclick()
            expect(page.locator('[data-kc-pending]')).to_have_text('2')
            check('double_click_decision_once',f.count('cleaner_manual_overrides')==before+1)
            page.locator('[data-kc-history-open]').click();expect(page.locator('[data-kc-history]')).to_contain_text('Решение владельца')
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-page="history"]')).to_be_visible();check('history_reload_deep_link')
            page.locator('[data-kc-page="history"] [data-kc-back]').click()
            # Profile draft and explicit activation are two independently saved commands.
            page.locator('[data-kc-profiles-open]').click();page.locator('[data-kc-profile-number]').fill('102');page.locator('[data-kc-profile-find] button').click()
            expect(page.locator('[data-kc-profile-form]')).to_be_visible();page.locator('[data-kc-profile-models]').select_option(['17 pro']);page.locator('[data-kc-profile-source]').fill('Тест совместимости <img src=x onerror="window.cleanerXss=2">');page.locator('[data-kc-profile-save]').click()
            expect(page.locator('[data-kc-profile-versions]')).to_contain_text('не активна');check('draft_does_not_activate',f.request('/profiles/102')[1]['active_version'] is None)
            page.get_by_role('button',name='Активировать версию',exact=True).click();expect(page.locator('[data-kc-profile-versions]')).to_contain_text('действует');check('explicit_profile_activation',f.request('/profiles/102')[1]['active_version']==1)
            check('profile_source_xss_is_text',page.locator('[data-kc-profile-versions] img').count()==0 and page.evaluate('window.cleanerXss===undefined'))
            page.goto(f.base_url+f.url.split(f.base_url)[1].split('?')[0]+'?tab=ads',wait_until='domcontentloaded');expect(page.locator('[data-ads-panel]')).to_be_visible();expect(page.locator('[data-keyword-cleaner]')).to_be_hidden();check('old_ads_deep_link_opens_bids')
            check('no_browser_js_errors',not errors);page.close()
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page();browser_login(page,f)
            before=f.count('cleaner_runs');page.locator('[data-kc-manual-advert]').select_option('10101');page.locator('[data-kc-manual-nm]').select_option('101');page.locator('[data-kc-run]').dblclick()
            expect(page.locator('[data-kc-manual-stage]')).to_be_visible()
            expect(page.locator('[data-kc-stage-text]')).to_contain_text('Получаем ключи')
            check('double_click_run_one_job',f.count('cleaner_runs')==before+1)
            def busy_owner_job(route):
                payload=route.fetch().json();payload.update(manual_worker_state='busy',manual_worker_alive=True)
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            page.route('**/keyword-cleaner/summary',busy_owner_job)
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-manual-stage]')).to_be_visible();check('manual_job_survives_reload')
            expect(page.locator('[data-kc-manual-advert]')).to_have_value('10101')
            expect(page.locator('[data-kc-manual-nm]')).to_have_value('101')
            expect(page.locator('[data-kc-selected-target]')).to_contain_text('Тестовая кампания 10101')
            expect(page.locator('[data-kc-selected-target]')).to_contain_text('Прозрачное стекло · iPhone 16 Pro Max')
            expect(page.locator('[data-kc-enabled-label]')).to_have_text('Выполняется ручная чистка')
            check('active_job_restores_exact_named_target_and_busy_label')
            page.route('**/keyword-cleaner/targets*',lambda route:route.abort())
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('[data-kc-target-note]')).to_have_text('Чистим только выбранную пару.')
            expect(page.locator('[data-kc-selected-target]')).to_contain_text('Тестовая кампания 10101')
            expect(page.locator('[data-kc-selected-target]')).to_contain_text('кампания 10101')
            check('job_identity_survives_targets_error')
            page.unroute('**/keyword-cleaner/targets*')
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-manual-advert]')).to_have_value('10101');expect(page.locator('[data-kc-manual-nm]')).to_have_value('101')
            check('manual_job_restores_without_second_post',f.count('cleaner_runs')==before+1)
            first=f.request('/summary')[1]['last_manual_job']['job_id']
            f.cleaner.record_manual_job(first,state='no_change',stage='finished',result='Нет новых допустимых фраз для исключения')
            code,new,_=f.request('/manual-clean',{'request_id':'another-tab-manual-job','advert_id':10101,'nm_id':101})
            check('second_tab_starts_new_job_after_terminal',code==202 and new['job_id']!=first)
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-manual-stage]')).to_be_visible()
            stored=page.evaluate("JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(key=>key.startsWith('wb-keyword-cleaner-manual-job:')))).job_id")
            check('server_latest_job_overrides_old_tab_storage',stored==new['job_id'])
            page.unroute_all(behavior='wait');page.close()
        with running_fixture('empty') as f:
            page=browser.new_page();browser_login(page,f)
            def late_history(route):
                payload=dict(items=[dict(kind='self_service_requested',created_at='2026-09-25T11:58:00Z',run_id='synthetic-late',facts=dict(job_id='synthetic-job',advert_id=10101,nm_id=101)),
                                    dict(kind='self_service_stage',created_at='2026-09-25T11:59:00Z',run_id='synthetic-late',facts=dict(job_id='synthetic-job',advert_id=10101,nm_id=101,state='running',stage='write_apply_claimed')),
                                    dict(kind='profile_draft',created_at='2026-09-25T11:59:30Z',facts=dict(nm_id=101)),
                                    dict(kind='run_finished',created_at='2026-09-25T12:00:00Z',run_id='synthetic-late',facts=dict(state='partial',summary=dict(dry_run=True))),
                                    dict(kind='self_service_finished',created_at='2026-09-25T12:04:00Z',run_id='synthetic-late',facts=dict(job_id='synthetic-job',advert_id=10101,nm_id=101,state='complete',stage='finished')),
                                    dict(kind='late_confirmation',created_at='2026-09-25T12:05:00Z',run_id='synthetic-late',facts=dict(confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=5,late=True,missing=0,state='confirmed',target='10101:101'))],next_cursor=None)
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            def late_run(route):
                phrases=[dict(target='10101:101',query='проверенный ключ '+str(i),state='confirmed',confirmed_at='2026-09-25T12:05:00Z',rule_id='WRONG_MODEL',reason='Другая модель') for i in range(5)]
                payload=dict(kind='manual_apply',state='partial',effective_state='complete',targets=[],phrases=phrases,write_operations=[],settlement=dict(confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=5))
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            page.route('**/keyword-cleaner/history*',late_history)
            page.route('**/keyword-cleaner/runs/synthetic-late',late_run)
            page.locator('[data-kc-history-open]').click()
            expect(page.locator('[data-kc-history]')).to_contain_text('Ручная чистка запущена')
            expect(page.locator('[data-kc-history]')).to_contain_text('Этап ручной чистки')
            expect(page.locator('[data-kc-history]')).to_contain_text('Проверка идёт · Проверяем результат записи в WB · кампания 10101 · товар WB 101')
            expect(page.locator('[data-kc-history]')).to_contain_text('Итог ручной чистки')
            expect(page.locator('[data-kc-history]')).to_contain_text('Чистка завершена · кампания 10101 · товар WB 101')
            check('self_service_history_has_target_and_stage_without_undefined','undefined' not in page.locator('[data-kc-history]').inner_text())
            expect(page.locator('[data-kc-history]')).to_contain_text('при ручном запуске: 5')
            expect(page.locator('[data-kc-history]')).to_contain_text('Первичный итог: Выполнено частично')
            expect(page.locator('[data-kc-history]')).not_to_contain_text('Без записи в WB')
            page.locator('[data-kc-history] button').first.click()
            expect(page.locator('[data-kc-run-detail]')).to_contain_text('Результат · Выполнено')
            expect(page.locator('[data-kc-run-detail]')).to_contain_text('при ручном запуске 5')
            check('late_confirmed_history_has_five_phrases',page.locator('[data-kc-run-detail] .kc-manual-group li').count()==5)
            check('late_confirmed_history_has_no_fake_empty_target','Обработанных кампаний пока нет' not in page.locator('[data-kc-run-detail]').inner_text())
            page.close()
        # The browser renders exact worker receipts without treating pending or
        # missing confirmation as an exclusion. HTTP is synthetic and local.
        for outcome in ('complete','partial','failed','detail-missing'):
            with running_fixture() as f:
                with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
                page=browser.new_page();browser_login(page,f);polls=[];rechecks=[]
                def manual_status(route):
                    polls.append(1);job_id=route.request.url.rsplit('/',1)[-1]
                    state='running' if len(polls)==1 else ('complete' if outcome=='detail-missing' else outcome)
                    body=dict(job_id=job_id,run_id='synthetic-scan',advert_id=10101,nm_id=101,state=state,
                              stage='classifying' if state=='running' else 'finished',updated_at='2026-09-25T12:00:0'+str(min(len(polls),9))+'Z',
                              scan_decisions=[dict(query='стекло iphone 16 pro max',verdict='allow',state='allow',observed_state='active',reason='Подходит товару',rule_id='MATCH'),
                                              dict(query='лишний ключ',verdict='exclude',state='pending_exclude',observed_state='excluded',reason='Другая модель',rule_id='WRONG_MODEL'),
                                              dict(query='старый ключ',verdict='exclude',state='already_excluded',observed_state='excluded',reason='Был исключён раньше',rule_id='OLD')])
                    if state=='complete':body.update(result='applied',write_run_id='synthetic-write')
                    if state=='partial':body.update(result='ambiguous',write_run_id='synthetic-write',error='WB ещё не подтвердил исключение',can_recheck=True,stage='write_apply_claimed')
                    if state=='failed':body.update(error='WB не ответил; запись не подтверждена')
                    route.fulfill(status=200,content_type='application/json',body=json.dumps(body,ensure_ascii=False))
                def write_detail(route):
                    if outcome=='detail-missing':route.abort();return
                    state='confirmed' if outcome=='complete' else 'submitted'
                    body=dict(run_id='synthetic-write',effective_state='complete' if outcome=='complete' else 'unresolved',
                              phrases=[dict(query='лишний ключ',state=state,rule_id='WRONG_MODEL',reason='Другая модель',target='10101:101',confirmed_at='2026-09-25T12:00:00Z' if state=='confirmed' else None)],write_operations=[])
                    route.fulfill(status=200,content_type='application/json',body=json.dumps(body,ensure_ascii=False))
                page.route('**/keyword-cleaner/manual-clean/*',manual_status)
                page.route('**/keyword-cleaner/runs/synthetic-write',write_detail)
                def recheck(route):
                    rechecks.append(route.request.post_data_json)
                    route.fulfill(status=202,content_type='application/json',body=json.dumps(dict(job_id=route.request.url.split('/')[-2],state='ambiguous',stage='write_apply_claimed')))
                page.route('**/keyword-cleaner/manual-clean/*/recheck',recheck)
                page.locator('[data-kc-manual-advert]').select_option('10101');page.locator('[data-kc-manual-nm]').select_option('101');page.locator('[data-kc-run]').click()
                expect(page.locator('[data-kc-manual-stage]')).to_be_visible()
                expect(page.locator('[data-kc-stage-text]')).to_contain_text('Сверяем правила товара',timeout=7000)
                if outcome=='complete':page.screenshot(path=str(output/'manual-running.png'),full_page=True);screens.append('manual-running.png')
                expect(page.locator('[data-kc-manual-result]')).to_contain_text('стекло iphone 16 pro max',timeout=7000)
                result_text=page.locator('[data-kc-manual-result]').inner_text()
                if outcome=='complete':
                    check('manual_confirmed_phrase_and_reason',all(value in result_text for value in ('WB подтвердил','лишний ключ','Другая модель','Оставлено')))
                    phrase_filter=page.locator('[data-kc-manual-result] select[aria-label="Фильтр ключей"]')
                    expect(page.locator('[data-kc-manual-result] .kc-phrase-detail')).not_to_contain_text('старый ключ')
                    phrase_filter.select_option('already')
                    already=page.locator('[data-kc-manual-result] .kc-phrase-detail')
                    expect(already).to_contain_text('старый ключ');expect(already).not_to_contain_text('лишний ключ')
                    expect(page.locator('[data-kc-manual-result] .kc-manual-stat').filter(has_text='Уже были исключены').locator('strong')).to_have_text('1')
                    check('newly_confirmed_not_counted_as_already_excluded')
                    expect(page.locator('[data-kc-summary]')).to_be_hidden()
                    expect(page.locator('[data-kc-selected-target]')).to_contain_text('Тестовая кампания 10101')
                    expect(page.locator('[data-kc-selected-target]')).to_contain_text('Прозрачное стекло · iPhone 16 Pro Max')
                    phrase_filter.select_option('all')
                    check('manual_reason_available_in_compact_table','Другая модель' in page.locator('[data-kc-manual-result] .kc-phrase-detail').inner_text())
                    page.locator('[data-kc-manual-advert]').select_option('10102');page.locator('[data-kc-manual-nm]').select_option('102')
                    expect(page.locator('[data-kc-selected-target]')).to_contain_text('Тестовая кампания 10102')
                    expect(page.locator('[data-kc-manual-result]')).to_contain_text('Кампания: Тестовая кампания 10101 · 10101')
                    check('terminal_card_keeps_original_job_target')
                if outcome=='partial':
                    check('manual_pending_is_not_confirmed','Ожидает WB' in result_text and 'лишний ключ' in result_text)
                    expect(page.get_by_role('button',name='Проверить результат WB')).to_be_visible()
                    expect(page.locator('[data-kc-run]')).to_be_disabled()
                    check('partial_waiting_recheck_blocks_new_clean')
                    page.get_by_role('button',name='Проверить результат WB').click()
                    expect(page.locator('[data-kc-message]')).to_contain_text('Уточняем ту же операцию и продолжаем пару только после проверки отправки.')
                    expect(page.locator('[data-kc-manual-stage]')).to_be_visible()
                    check('recheck_uses_same_job_readback_only',len(rechecks)==1 and bool(rechecks[0].get('request_id')) and f.count('cleaner_runs')==2)
                if outcome=='failed':check('manual_error_is_visible','Чистка не выполнена' in result_text and 'WB не ответил' in result_text)
                if outcome=='detail-missing':
                    expect(page.locator('[data-kc-manual-result]')).to_contain_text('Подтверждения WB ещё не удалось загрузить')
                    expect(page.locator('[data-kc-manual-result] .kc-manual-group').filter(has=page.get_by_role('heading',name='Уже были в исключениях'))).to_have_count(0)
                    expect(page.locator('[data-kc-manual-result] .kc-manual-stat').filter(has_text='Уже были исключены').locator('strong')).to_have_text('—')
                    check('missing_write_detail_does_not_invent_already_excluded')
                check('manual_result_has_no_script',page.locator('[data-kc-manual-result] script').count()==0)
                if outcome=='complete':
                    page.screenshot(path=str(output/'manual-result.png'),full_page=True);screens.append('manual-result.png')
                    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'manual-result-mobile.png'),full_page=True);screens.append('manual-result-mobile.png')
                    check('manual_result_mobile_no_overflow',page.locator('[data-keyword-cleaner]').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1'))
                page.close()
        # Batch selection is read-only until one explicit durable command. The
        # routes below are synthetic receipts, so no WB or worker is contacted.
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page(viewport={'width':1440,'height':1080});posts=[];polls=[];rechecks=[];state={'rechecked':False}
            candidates=[]
            for i in range(24):
                status='active' if i<16 else 'paused' if i<20 else 'completed' if i<22 else 'archive'
                candidates.append(dict(advert_id=10100+i,nm_id=20100+i,campaign_name=f'Кампания Стекло {i+1}',product_title=f'Защитное стекло iPhone 16 Pro Max · цвет {i+1}',status=status,status_code={'active':9,'paused':11,'completed':7,'archive':-1}[status],payment_type='cpm',eligible=i<20,reason=None if i<20 else 'unsupported_campaign_status',admitted=True,profile_ready=True))
            categories={name:dict(selectable=name in ('active','paused'),reason=None if name in ('active','paused') else 'unsupported_campaign_status') for name in ('active','paused','completed','archive')}
            def eligibility(route):route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(items=candidates,loading=False,error=None,categories=categories,counts=dict(total=24,eligible=20,profile_required=0,ineligible=4,unknown=0,selectable_active=16,selectable_paused=4)),ensure_ascii=False))
            def summary_with_batch(route):
                payload=route.fetch().json()
                if posts:
                    payload['last_manual_batch']=dict(batch_id='synthetic-batch',state='attention_required' if not state['rechecked'] else 'partial',created_at='2026-09-25T13:00:00Z',updated_at='2026-09-25T13:05:00Z')
                    payload['last_manual_job']=dict(job_id='child-1',advert_id=10101,nm_id=20101,state='partial',stage='write_apply_claimed',can_recheck=not state['rechecked'],created_at='2026-09-25T13:01:00Z',updated_at='2026-09-25T13:04:00Z')
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            def batch_post(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=202,content_type='application/json',body=json.dumps(dict(batch_id='synthetic-batch',state='queued',selected_count=len(posts[-1]['targets']),created_at='2026-09-25T13:00:00Z')))
            def batch_status(route):
                polls.append(1);selected=[next(item for item in candidates if item['advert_id']==row['advert_id'] and item['nm_id']==row['nm_id']) for row in posts[0]['targets']]
                terminal=state['rechecked'];phase='partial' if terminal else 'running' if len(polls)==1 else 'attention_required'
                items=[]
                for index,item in enumerate(selected):
                    row=dict(index=index,advert_id=item['advert_id'],nm_id=item['nm_id'],campaign_name=item['campaign_name'],product_title=item['product_title'],selected_status=item['status'],state='queued',stage='fetching',job_id=None,write_run_id=None,new_checked=None,confirmed_excluded=None,allowed=None,review_count=None,pending_count=None,already_excluded=None)
                    if index==0:row.update(state='complete',job_id='child-0',write_run_id='write-0',new_checked=31,confirmed_excluded=5,allowed=2,review_count=15,pending_count=0,already_excluded=9)
                    if index==1:row.update(state='complete' if terminal else 'partial',job_id='child-1',write_run_id='write-1',new_checked=1,confirmed_excluded=1 if terminal else None,pending_count=0 if terminal else 1)
                    if terminal and index==2:row.update(state='failed',error='WB не ответил',error_code='read_timeout')
                    if terminal and index==3:row.update(state='no_change',new_checked=0,confirmed_excluded=0,allowed=0,review_count=0,pending_count=0,already_excluded=0)
                    if terminal and index==4:row.update(state='skipped',error='Кампания больше недоступна')
                    if terminal and index==5:row.update(state='partial',new_checked=1,pending_count=1,error='WB ещё не подтвердил результат')
                    if terminal and index>5:row.update(state='complete',new_checked=0,confirmed_excluded=0,allowed=0,review_count=0,pending_count=0,already_excluded=0)
                    items.append(row)
                payload=dict(batch_id='synthetic-batch',state=phase,stage='write_apply_claimed',selected_count=len(selected),done_count=len(selected) if terminal else 1,confirmed_count=12 if terminal else None,no_change_count=1 if terminal else None,partial_count=1 if terminal else None,failed_count=1 if terminal else None,skipped_count=1 if terminal else None,current_index=1,current_target=selected[1],created_at='2026-09-25T13:00:00Z',updated_at='2026-09-25T13:05:00Z',items=items)
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            def batch_detail(route):
                index=int(route.request.url.rsplit('/',1)[-1]);item=dict(index=index)
                if index==0:
                    scan=[dict(query=f'новый {i}',verdict='exclude',observed_state='excluded',reason='Другая модель') for i in range(5)]+[dict(query=f'оставлен {i}',verdict='allow',observed_state='active',reason='Подходит товару') for i in range(2)]+[dict(query=f'старый {i}',verdict='exclude',observed_state='excluded',reason='Был исключён раньше') for i in range(9)]+[dict(query=f'разбор {i}',verdict='review',observed_state='active',reason='Требует решения') for i in range(15)]
                    phrases=[dict(query=f'новый {i}',state='confirmed',confirmed_at='2026-09-25T13:03:00Z',reason='Другая модель') for i in range(5)]
                    payload=dict(item=item,job=dict(scan_decisions=scan,write_run_id='write-0',state='complete'),run=dict(phrases=phrases))
                elif index==1:payload=dict(item=item,job=dict(job_id='child-1',state='complete' if state['rechecked'] else 'partial',can_recheck=not state['rechecked'],write_run_id='write-1',scan_decisions=[dict(query='ожидающий ключ',verdict='exclude',observed_state='excluded' if state['rechecked'] else 'active',reason='Другая модель')]),run=dict(phrases=[dict(query='ожидающий ключ',state='confirmed' if state['rechecked'] else 'submitted',confirmed_at='2026-09-25T13:06:00Z' if state['rechecked'] else None,reason='Другая модель')]))
                else:payload=dict(item=item,job=None,run=None)
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            def batch_recheck(route):
                rechecks.append(route.request.post_data_json);state['rechecked']=True
                route.fulfill(status=202,content_type='application/json',body=json.dumps(dict(job_id='child-1',state='ambiguous',stage='write_apply_claimed')))
            page.route('**/keyword-cleaner/manual-batches/eligibility*',eligibility)
            page.route('**/keyword-cleaner/manual-batches/synthetic-batch/items/*',batch_detail)
            page.route('**/keyword-cleaner/manual-batches/synthetic-batch',batch_status)
            page.route('**/keyword-cleaner/manual-batches',batch_post)
            page.route('**/keyword-cleaner/manual-clean/child-1/recheck',batch_recheck)
            page.route('**/keyword-cleaner/summary',summary_with_batch)
            browser_login(page,f)
            page.locator('[data-kc-batch-open]').click();expect(page.locator('[data-kc-batch-dialog]')).to_be_visible()
            expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано пар: 16')
            expect(page.locator('[data-kc-batch-overview]')).to_have_text('CPM-пары: всего 24 · готовы 20 · требуют настройки 0')
            check('batch_overview_counts_exact_cpm_pairs_only')
            expect(page.locator('[data-kc-batch-category="active"]')).to_be_checked()
            expect(page.locator('[data-kc-batch-category="paused"]')).not_to_be_checked()
            expect(page.locator('[data-kc-batch-category="completed"]')).to_be_disabled()
            expect(page.locator('[data-kc-batch-category="archive"]')).to_be_disabled()
            category_text=page.locator('[data-kc-batch-categories]').inner_text()
            check('batch_category_controls_visible_with_exact_counts',all(value in category_text for value in ('Активные · всего 16','Готовы 16 · выбрано 16','Приостановленные · всего 4','Готовы 4 · выбрано 0')))
            expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
            check('batch_final_button_visible_without_dialog_scroll',page.locator('[data-kc-batch-start]').evaluate('(node)=>{const r=node.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight}'))
            check('batch_active_default_paused_optional_and_no_autostart',len(posts)==0 and page.locator('[data-kc-batch-choices] input').count()==20 and 'Кампания Стекло 21' not in page.locator('[data-kc-batch-choices]').inner_text())
            page.screenshot(path=str(output/'batch-selection.png'),full_page=True);screens.append('batch-selection.png')
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'batch-selection-mobile.png'),full_page=True);screens.append('batch-selection-mobile.png')
            check('batch_modal_mobile_fits_viewport',page.locator('[data-kc-batch-start]').evaluate('(button)=>{const b=button.getBoundingClientRect(),d=button.closest("dialog").getBoundingClientRect(),inner=button.closest(".kc-batch-dialog-inner").getBoundingClientRect();return d.left>=0&&d.right<=innerWidth+1&&b.top>=d.top&&b.bottom<=d.bottom-4&&b.bottom<=inner.bottom&&b.bottom<=innerHeight}'))
            page.set_viewport_size({'width':1440,'height':1080})
            page.locator('[data-kc-batch-choices] input[value="10116:20116"]').check()
            page.locator('[data-kc-batch-choices] input[value="10100:20100"]').uncheck()
            page.locator('[data-kc-batch-close]').click();page.reload(wait_until='domcontentloaded');page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-choices] input[value="10116:20116"]')).to_be_checked()
            expect(page.locator('[data-kc-batch-choices] input[value="10100:20100"]')).not_to_be_checked()
            check('batch_explicit_pair_selection_survives_reload',len(posts)==0)
            full_candidates=candidates;candidates=[item for item in candidates if item['advert_id']!=10116]
            page.locator('[data-kc-batch-close]').click();page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-choices]')).to_contain_text('Нет в текущем подтверждённом каталоге')
            expect(page.locator('[data-kc-batch-start]')).to_be_disabled()
            check('batch_missing_selected_pair_is_visible_and_blocks_submit',len(posts)==0)
            candidates=full_candidates;page.locator('[data-kc-batch-close]').click();page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
            page.locator('[data-kc-batch-start]').click();expect(page.locator('[data-kc-batch-stage]')).to_be_visible()
            check('batch_one_durable_post_with_exact_pairs',len(posts)==1 and len(posts[0]['targets'])==16 and posts[0]['selected_categories']==['active','paused'] and bool(posts[0]['request_id']))
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('Кампания Стекло',timeout=8000)
            page.screenshot(path=str(output/'batch-running.png'),full_page=True);screens.append('batch-running.png')
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Требуется уточнение',timeout=8000)
            expect(page.locator('[data-kc-review-batch-note]')).to_be_visible()
            expect(page.locator('[data-kc-reviews]').get_by_role('button',name='Оставить').first).to_be_disabled()
            expect(page.locator('[data-kc-reviews]').get_by_role('button',name='Исключить').first).to_be_disabled()
            check('batch_blocks_conflicting_owner_review_decisions')
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-batch-result]')).to_contain_text('Требуется уточнение',timeout=8000)
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Кампания Стекло 2')
            check('batch_restores_parent_over_newer_child_after_reload',len(posts)==1 and page.locator('[data-kc-manual-result]').inner_text()=='')
            page.locator('[data-kc-batch-result] button').nth(1).click()
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Уточнить результат и продолжить')
            page.get_by_role('button',name='Уточнить результат и продолжить').click()
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Массовая чистка · Частично',timeout=8000)
            expect(page.locator('[data-kc-batch-result] .kc-phrase-detail')).to_contain_text('WB подтвердил',timeout=8000)
            expect(page.get_by_role('button',name='Уточнить результат и продолжить')).to_have_count(0)
            check('batch_recheck_refreshes_same_child_without_new_batch_post',len(rechecks)==1 and bool(rechecks[0].get('request_id')) and len(posts)==1)
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-batch-result]')).to_contain_text('Массовая чистка · Частично',timeout=8000)
            check('batch_terminal_parent_survives_newer_child_reload',page.locator('[data-kc-manual-result]').inner_text()=='')
            check('batch_parent_table_has_ten_pairs_per_page',page.locator('[data-kc-batch-result] tbody tr').count()==10)
            page.locator('[data-kc-batch-result] > .kc-manual-card > .kc-result-pager').get_by_role('button',name='Далее').click()
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Пары 11–16 из 16')
            check('batch_parent_pagination_reaches_remaining_pairs',page.locator('[data-kc-batch-result] tbody tr').count()==6)
            page.locator('[data-kc-batch-result] > .kc-manual-card > .kc-result-pager').get_by_role('button',name='Назад').click()
            page.locator('[data-kc-batch-result] button').first.click()
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('новый 0')
            expect(page.locator('[data-kc-batch-result] .kc-phrase-detail')).not_to_contain_text('старый 0')
            page.locator('[data-kc-batch-result] select[aria-label="Фильтр ключей"]').select_option('already')
            expect(page.locator('[data-kc-batch-result] .kc-phrase-detail')).to_contain_text('старый 0')
            expect(page.locator('[data-kc-batch-result] .kc-phrase-detail')).not_to_contain_text('новый 0')
            page.locator('[data-kc-batch-result] select[aria-label="Фильтр ключей"]').select_option('all')
            page.locator('[data-kc-batch-result] .kc-phrase-detail').get_by_role('button',name='Далее').click()
            expect(page.locator('[data-kc-batch-result] .kc-phrase-detail')).to_contain_text('разбор 14')
            check('batch_compact_details_filter_and_paginate')
            page.screenshot(path=str(output/'batch-result.png'),full_page=True);screens.append('batch-result.png')
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'batch-result-mobile.png'),full_page=True);screens.append('batch-result-mobile.png')
            check('batch_mobile_no_page_overflow',page.locator('[data-keyword-cleaner]').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1'))
            page.unroute_all(behavior='wait');page.close()
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page();posts=[]
            only=dict(advert_id=10101,nm_id=101,campaign_name='Тестовая кампания 10101',product_title='Стекло iPhone 16 Pro Max',status='active',status_code=9,eligible=True,reason=None,payment_type='cpm')
            eligible=dict(items=[only],loading=False,error=None,counts=dict(total=1,eligible=1,profile_required=0,ineligible=0,unknown=0,selectable_active=1,selectable_paused=0),categories={status:dict(selectable=status in ('active','paused'),reason='unsupported_campaign_status' if status in ('completed','archive') else None) for status in ('active','paused','completed','archive')})
            page.route('**/keyword-cleaner/manual-batches/eligibility*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(eligible,ensure_ascii=False)))
            def lost_batch_post(route):posts.append(route.request.post_data_json);route.abort('failed')
            page.route('**/keyword-cleaner/manual-batches',lost_batch_post)
            def accepted_summary(route):
                payload=route.fetch().json()
                if posts:payload['last_manual_batch']=dict(batch_id='lost-batch',state='complete',created_at='2026-09-25T14:00:00Z',updated_at='2026-09-25T14:01:00Z')
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            page.route('**/keyword-cleaner/summary',accepted_summary)
            page.route('**/keyword-cleaner/manual-batches/lost-batch',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(batch_id='lost-batch',state='complete',stage='finished',selected_count=1,done_count=1,confirmed_count=0,no_change_count=1,partial_count=0,failed_count=0,skipped_count=0,created_at='2026-09-25T14:00:00Z',updated_at='2026-09-25T14:01:00Z',items=[dict(index=0,**only,selected_status='active',state='no_change',new_checked=0,confirmed_excluded=0,allowed=0,review_count=0,pending_count=0,already_excluded=0)]))))
            page.route('**/keyword-cleaner/requests/*',lambda route:route.abort())
            browser_login(page,f);page.locator('[data-kc-batch-open]').click();expect(page.locator('[data-kc-batch-start]')).to_be_enabled();page.locator('[data-kc-batch-start]').click()
            expect(page.locator('[data-kc-recover]')).to_be_visible(timeout=18000);check('batch_lost_reply_keeps_one_durable_request',len(posts)==1)
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-batch-result]')).to_contain_text('Массовая чистка · Выполнено',timeout=8000)
            page.unroute('**/keyword-cleaner/requests/*')
            page.route('**/keyword-cleaner/requests/*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(batch_id='lost-batch',state='complete',selected_count=1,created_at='2026-09-25T14:00:00Z'))))
            page.locator('[data-kc-recover]').click();expect(page.locator('[data-kc-recover]')).to_be_hidden()
            check('batch_lost_reply_recovered_by_get_without_repost',len(posts)==1 and posts[0]['targets']==[dict(advert_id=10101,nm_id=101)])
            page.unroute_all(behavior='wait');page.close()
        with running_fixture() as f:
            page=browser.new_page();browser_login(page,f)
            before=f.count('cleaner_requests');page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-choices] input[value="10101:101"]')).to_be_checked()
            expect(page.locator('[data-kc-batch-choices] input[value="10102:102"]')).to_be_disabled()
            expect(page.locator('[data-kc-batch-choices] input[value="10103:101"]')).to_have_count(0)
            expect(page.locator('[data-kc-batch-categories] input[data-kc-batch-category="completed"]')).to_be_disabled()
            check('real_local_eligibility_exposes_exact_denials_without_submit',f.count('cleaner_requests')==before)
            page.close()
        with running_fixture() as f:
            page=browser.new_page()
            rows=[dict(advert_id=aid,nm_id=101,campaign_name=name,product_title='Стекло Pro Max',status=status,payment_type='cpm',eligible=eligible,reason=reason,admitted=True,profile_ready=eligible) for aid,name,status,eligible,reason in (
                (201,'Готовая активная','active',True,None),(202,'Нужна настройка','active',False,'profile_required'),
                (203,'Готовая пауза','paused',True,None),(204,'Старый архив','archive',False,'unsupported_campaign_status'))]
            rows.append(dict(advert_id=205,nm_id=101,campaign_name='CPC не участвует',product_title='Стекло',status='active',payment_type='cpc',eligible=True,reason=None,admitted=True,profile_ready=True))
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            categories={status:dict(selectable=status in ('active','paused'),reason='unsupported_campaign_status' if status in ('completed','archive') else None) for status in ('active','paused','completed','archive')}
            payload=dict(items=rows,counts=dict(total=4,eligible=2,selectable_active=1,selectable_paused=1,profile_required=1,ineligible=1,unknown=None),loading=False,error=None,categories=categories)
            page.route('**/keyword-cleaner/manual-batches/eligibility*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False)))
            browser_login(page,f);before=f.count('cleaner_requests');page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-overview]')).to_have_text('CPM-пары: получено 4 · готовы 2 · требуют настройки 1 · без подтверждения —')
            names=page.locator('[data-kc-batch-choices] strong').all_text_contents()
            check('batch_cpm_counts_eligible_first_archive_compact',names==['Готовая активная','Готовая пауза','Нужна настройка'] and f.count('cleaner_requests')==before)
            expect(page.locator('[data-kc-batch-choices] input[value="205:101"]')).to_have_count(0)
            expect(page.locator('[data-kc-batch-choices] input[value="201:101"]')).to_be_checked()
            expect(page.locator('[data-kc-batch-choices] input[value="203:101"]')).not_to_be_checked()
            expect(page.locator('[data-kc-batch-choices] input[value="202:101"]')).to_be_disabled()
            page.locator('[data-kc-batch-choices] input[value="203:101"]').check()
            expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано пар: 2')
            check('batch_paused_explicit_and_profile_required_not_selected',f.count('cleaner_requests')==before)
            page.screenshot(path=str(output/'batch-cpm-overview.png'),full_page=True);screens.append('batch-cpm-overview.png')
            page.close()
        with running_fixture() as f:
            page=browser.new_page();posts=[]
            large=[dict(advert_id=30000+i,nm_id=40000+i,campaign_name=f'CPM {i+1}',product_title='Стекло',status='active',payment_type='cpm',eligible=True,reason=None,admitted=True,profile_ready=True) for i in range(130)]
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            categories={status:dict(selectable=status in ('active','paused'),reason='unsupported_campaign_status' if status in ('completed','archive') else None) for status in ('active','paused','completed','archive')}
            payload=dict(items=large,counts=dict(total=130,eligible=130,selectable_active=130,selectable_paused=0,profile_required=0,ineligible=0,unknown=0),loading=False,error=None,categories=categories)
            page.route('**/keyword-cleaner/manual-batches/eligibility*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False)))
            def large_post(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=202,content_type='application/json',body=json.dumps(dict(batch_id='synthetic-large',state='queued',selected_count=130,created_at='2026-09-26T00:00:00Z')))
            page.route('**/keyword-cleaner/manual-batches',large_post)
            page.route('**/keyword-cleaner/manual-batches/synthetic-large',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(batch_id='synthetic-large',state='complete',selected_count=130,done_count=130,confirmed_count=0,no_change_count=130,partial_count=0,failed_count=0,skipped_count=0,updated_at='2026-09-26T00:01:00Z',items=[]))))
            browser_login(page,f);before=f.count('cleaner_requests');page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано пар: 130')
            expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
            page.locator('[data-kc-batch-start]').click()
            expect(page.locator('[data-kc-batch-stage]')).to_be_visible()
            check('batch_large_selection_one_explicit_intent_no_ui_cap',len(posts)==1 and len(posts[0]['targets'])==130 and bool(posts[0]['request_id']) and f.count('cleaner_requests')==before)
            page.close()
        with running_fixture() as f:
            page=browser.new_page();calls=[];retries=[]
            categories={status:dict(selectable=status in ('active','paused'),reason='unsupported_campaign_status' if status in ('completed','archive') else None) for status in ('active','paused','completed','archive')}
            def delayed_eligibility(route):
                refresh='refresh=1' in route.request.url;calls.append(refresh)
                if refresh:
                    retries.append(True)
                    if len(retries)==1:
                        route.abort('failed');return
                payload=dict(items=[],loading=not refresh and len(calls)<3,error='campaign_catalog_unavailable' if not refresh and len(calls)>=3 else None,categories=categories,counts={key:(0 if refresh else None) for key in ('total','eligible','profile_required','ineligible','unknown','selectable_active','selectable_paused')})
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            page.route('**/keyword-cleaner/manual-batches/eligibility*',delayed_eligibility)
            browser_login(page,f);before=f.count('cleaner_requests');page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-choices]')).to_contain_text('Загружаем точный список')
            expect(page.locator('[data-kc-batch-choices]')).not_to_contain_text('Доступных пар пока нет')
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Активные · всего —')
            expect(page.locator('[data-kc-batch-overview]')).to_contain_text('всего — · готовы — · требуют настройки —')
            check('batch_pending_counts_unknown_and_no_false_empty')
            expect(page.locator('[data-kc-batch-choices]')).to_contain_text('Не удалось загрузить список кампаний. Повторите загрузку.',timeout=8000)
            expect(page.get_by_role('button',name='Повторить загрузку')).to_be_visible()
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Активные · всего —')
            expect(page.locator('[data-kc-batch-overview]')).to_contain_text('всего — · готовы — · требуют настройки —')
            expect(page.locator('[data-kc-batch-start]')).to_be_disabled()
            check('batch_error_visible_counts_unknown_and_no_false_empty','Доступных пар пока нет' not in page.locator('[data-kc-batch-choices]').inner_text())
            failed_calls=len(calls);page.wait_for_timeout(2300)
            check('batch_error_stops_automatic_polling',len(calls)==failed_calls and f.count('cleaner_requests')==before)
            page.get_by_role('button',name='Повторить загрузку').click()
            expect(page.locator('[data-kc-batch-choices]')).to_contain_text('Список кампаний сейчас недоступен')
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Активные · всего —')
            failed_calls=len(calls);page.wait_for_timeout(2300)
            check('batch_network_error_after_loading_stops_polling',len(calls)==failed_calls)
            page.get_by_role('button',name='Повторить загрузку').click()
            expect(page.locator('[data-kc-batch-choices]')).to_contain_text('Активных и приостановленных CPM-пар пока нет')
            check('batch_explicit_retry_only_then_true_empty',calls[-1] is True and len(retries)==2 and f.count('cleaner_requests')==before)
            page.close()
        # Each variant is rendered by the real app from isolated synthetic SQL state.
        for mode,status in [('empty','Выполнено'),('partial','Выполнено частично'),('failed','Не выполнено'),('unresolved','Проверяем результат WB'),('rejected','Не выполнено'),('profile-required','Выполнено частично')]:
            with running_fixture(mode) as f:
                page=browser.new_page(viewport={'width':1280,'height':960})
                if mode=='failed':
                    page.request.post(f.base_url+'/login',form={'username':'owner','password':PASSWORD,'next':f.url.split(f.base_url)[1]})
                    page.goto(f.url,wait_until='domcontentloaded')
                else: browser_login(page,f)
                expect(page.locator('[data-kc-status]')).to_have_text(status)
                if mode in {'partial','failed','unresolved','rejected'}:check(mode+'_not_success',page.locator('[data-kc-status]').get_attribute('data-tone')!='success')
                if mode=='empty':expect(page.locator('[data-kc-reviews]')).to_contain_text('Все вопросы разобраны');check('empty_review_state')
                if mode=='failed':check('missing_counts_are_not_zero',page.locator('[data-kc-checked]').inner_text()=='—')
                if mode=='unresolved':
                    expect(page.locator('[data-kc-reviews]')).to_contain_text('Все вопросы разобраны');expect(page.locator('[data-kc-indicator]')).to_be_visible();check('unresolved_remains_when_reviews_empty')
                    expect(page.locator('[data-kc-enabled]')).to_have_count(0);check('manual_mode_does_not_offer_scheduler_toggle')
                if mode=='rejected':
                    expect(page.locator('[data-kc-alerts]')).to_contain_text('Не выполнено: WB не принял исключение. Ключей: 1.')
                    expect(page.locator('[data-kc-excluded]')).to_have_text('0')
                    check('rejected_is_not_confirmed_and_reason_visible')
                if mode=='profile-required':
                    expect(page.locator('[data-kc-alerts]')).to_contain_text('Нужна настройка товаров');page.locator('[data-kc-profiles-open]').click();expect(page.locator('[data-kc-profiles]')).to_contain_text('Товар WB 102');check('unknown_profile_visible_setting')
                filename=mode+'.png';page.screenshot(path=str(output/filename),full_page=True);screens.append(filename);page.close()
        with running_fixture('empty') as f:
            page=browser.new_page();browser_login(page,f,'reader');expect(page.locator('[data-kc-run]')).to_be_disabled();expect(page.locator('[data-kc-alerts]')).to_contain_text('только назначенному владельцу');check('reader_view_is_read_only')
            before=f.count('cleaner_requests');response=page.request.post(f.base_url+PREFIX+'/runs',data={'request_id':'browser-reader-direct'},headers={'Origin':f.base_url,'Content-Type':'application/json','X-WB-Keyword-Cleaner-CSRF':'1'});check('browser_reader_direct_post_forbidden',response.status==403 and f.count('cleaner_requests')==before);page.close()
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page()
            def worker_down(route):
                payload=route.fetch().json();payload.update(manual_worker_state='down',manual_worker_alive=False)
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            before=f.count('cleaner_requests');page.route('**/keyword-cleaner/summary',worker_down);browser_login(page,f)
            expect(page.locator('[data-kc-target-note]')).to_contain_text('Исполнитель ручной чистки не отвечает')
            expect(page.locator('[data-kc-run]')).to_be_disabled()
            check('worker_down_prevents_manual_submit',f.count('cleaner_requests')==before)
            page.unroute_all(behavior='wait');page.close()
        # A lost manual-run response is recovered only by GET of the original
        # command. A hanging response observes the same one-submit invariant.
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page();browser_login(page,f);posts=[]
            def pending_post(route):
                posts.append(route.request.post_data_json);assert route.fetch().status==202;route.abort('failed')
            page.route('**/keyword-cleaner/manual-clean',pending_post);page.route('**/keyword-cleaner/requests/*',lambda route:route.abort())
            page.locator('[data-kc-manual-advert]').select_option('10101');page.locator('[data-kc-manual-nm]').select_option('101');page.locator('[data-kc-run]').click();expect(page.locator('[data-kc-recover]')).to_be_visible();expect(page.locator('[data-kc-message]')).to_contain_text('Не удалось проверить сохранение')
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-recover]')).to_be_visible();check('manual_uncertain_command_survives_reload')
            page.unroute('**/keyword-cleaner/requests/*');page.locator('[data-kc-recover]').click();expect(page.locator('[data-kc-recover]')).to_be_hidden();check('manual_recovery_get_only_one_post',len(posts)==1);page.unroute_all(behavior='wait');page.close()
        with running_fixture() as f:
            with f.cleaner.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.cleaner.key,))
            page=browser.new_page();browser_login(page,f);posts=[];hanging=[]
            def hanging_post(route):
                hanging.append(route);posts.append(route.request.post_data_json);assert route.fetch().status==202
            page.route('**/keyword-cleaner/manual-clean',hanging_post);page.locator('[data-kc-manual-advert]').select_option('10101');page.locator('[data-kc-manual-nm]').select_option('101')
            start=time.monotonic();page.locator('[data-kc-run]').click();expect(page.locator('[data-kc-stage-text]')).to_contain_text('Получаем ключи',timeout=16000)
            check('manual_hanging_post_recovers_one_request',len(posts)==1 and 9<=time.monotonic()-start<16)
            for route in hanging:
                try:route.abort('failed')
                except Exception:pass
            page.unroute_all(behavior='ignoreErrors');page.close()
        browser.close()
    from apps.search_cluster_cleaner_batch_recovery_browser_smoke import run as run_batch_recovery
    recovery = run_batch_recovery(output/'batch-recovery')
    checks.extend('batch_recovery_'+name for name in recovery['checks'])
    from apps.search_cluster_cleaner_drift_batch_browser_smoke import main as drift_batch_browser_main
    drift_batch_browser_main()
    checks.append('drift_batch_explicit_resume_reload_and_final_counts')
    return dict(passed=len(checks),checks=checks,screenshots=[str(output/name) for name in screens],wb_writes=0,synthetic_wb_posts=2)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args();args.output=args.output or Path(tempfile.mkdtemp(prefix='wbc-cleaner-browser-'));result=run(args.output);(args.output/'browser-receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
