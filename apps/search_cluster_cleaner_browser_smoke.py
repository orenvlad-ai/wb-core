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
            page.close()
        with running_fixture('empty') as f:
            page=browser.new_page();browser_login(page,f)
            def late_history(route):
                payload=dict(items=[dict(kind='run_finished',created_at='2026-09-25T12:00:00Z',run_id='synthetic-late',facts=dict(state='partial',summary=dict(dry_run=True))),
                                    dict(kind='late_confirmation',created_at='2026-09-25T12:05:00Z',run_id='synthetic-late',facts=dict(confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=5,late=True,missing=0,state='confirmed',target='10101:101'))],next_cursor=None)
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            def late_run(route):
                phrases=[dict(target='10101:101',query='проверенный ключ '+str(i),state='confirmed',confirmed_at='2026-09-25T12:05:00Z',rule_id='WRONG_MODEL',reason='Другая модель') for i in range(5)]
                payload=dict(kind='manual_apply',state='partial',effective_state='complete',targets=[],phrases=phrases,write_operations=[],settlement=dict(confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=5))
                route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
            page.route('**/keyword-cleaner/history*',late_history)
            page.route('**/keyword-cleaner/runs/synthetic-late',late_run)
            page.locator('[data-kc-history-open]').click()
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
                    check('manual_confirmed_phrase_and_reason',all(value in result_text for value in ('WB подтвердил исключение','лишний ключ','Другая модель','Оставлено')))
                    already=page.locator('[data-kc-manual-result] .kc-manual-group').filter(has=page.get_by_role('heading',name='Уже были в исключениях'))
                    expect(already).to_contain_text('старый ключ');expect(already).not_to_contain_text('лишний ключ')
                    expect(page.locator('[data-kc-manual-result] .kc-manual-stat').filter(has_text='Уже были исключены').locator('strong')).to_have_text('1')
                    check('newly_confirmed_not_counted_as_already_excluded')
                    expect(page.locator('[data-kc-summary]')).to_be_hidden()
                    expect(page.locator('[data-kc-selected-target]')).to_contain_text('Тестовая кампания 10101')
                    expect(page.locator('[data-kc-selected-target]')).to_contain_text('Прозрачное стекло · iPhone 16 Pro Max')
                    page.locator('[data-kc-manual-result] details').last.locator('summary').click()
                    check('manual_rule_id_available_in_details','WRONG_MODEL' in page.locator('[data-kc-manual-result]').inner_text())
                    page.locator('[data-kc-manual-result] details').last.locator('summary').click()
                    page.locator('[data-kc-manual-advert]').select_option('10102');page.locator('[data-kc-manual-nm]').select_option('102')
                    expect(page.locator('[data-kc-selected-target]')).to_contain_text('Тестовая кампания 10102')
                    expect(page.locator('[data-kc-manual-result]')).to_contain_text('Кампания: Тестовая кампания 10101 · 10101')
                    check('terminal_card_keeps_original_job_target')
                if outcome=='partial':
                    check('manual_pending_is_not_confirmed','Ожидают подтверждения WB' in result_text and 'WB подтвердил исключение' not in result_text)
                    expect(page.get_by_role('button',name='Проверить результат WB')).to_be_visible()
                    expect(page.locator('[data-kc-run]')).to_be_disabled()
                    check('partial_waiting_recheck_blocks_new_clean')
                    page.get_by_role('button',name='Проверить результат WB').click()
                    expect(page.locator('[data-kc-message]')).to_contain_text('Повторно читаем результат WB')
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
            page.close()
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
            page.unroute('**/keyword-cleaner/requests/*');page.locator('[data-kc-recover]').click();expect(page.locator('[data-kc-recover]')).to_be_hidden();check('manual_recovery_get_only_one_post',len(posts)==1);page.close()
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
    return dict(passed=len(checks),checks=checks,screenshots=[str(output/name) for name in screens],wb_writes=0,synthetic_wb_posts=2)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args();args.output=args.output or Path(tempfile.mkdtemp(prefix='wbc-cleaner-browser-'));result=run(args.output);(args.output/'browser-receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
