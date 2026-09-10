#!/usr/bin/env python3
"""Real browser + authenticated local HTTP; synthetic data and transport faults."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
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
        with running_fixture() as f:
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
            # An actual accepted POST loses its browser response. Recovery reads the
            # original server-saved request, never resubmits or changes permissions.
            posts=[];recovery=[]
            page.on('request',lambda req:recovery.append(req.url) if '/requests/' in req.url else None)
            def lose_response(route):
                if route.request.method!='POST':return route.continue_()
                posts.append(route.request.post_data_json)
                response=route.fetch();assert response.status==202
                route.abort('failed')
            page.route('**/keyword-cleaner/settings',lose_response)
            before=f.count('cleaner_requests');page.locator('[data-kc-time-edit]').click();page.locator('[data-kc-time]').fill('08:15');page.locator('[data-kc-time-save]').click()
            expect(page.locator('[data-kc-time-edit]')).to_have_text('08:15')
            expect(page.locator('[data-kc-recover]')).to_be_hidden(timeout=15000)
            page.wait_for_function("document.querySelector('[data-kc-message]').textContent.includes('Изменения сохранены')")
            check('lost_post_response_one_submit_then_original_request_get',len(posts)==1 and f.count('cleaner_requests')==before+1 and any(posts[0]['request_id'] in url for url in recovery))
            page.unroute('**/keyword-cleaner/settings',lose_response)
            # Revisions conflict through HTTP while the owner is editing an older view.
            page.locator('[data-kc-time-edit]').click();page.locator('[data-kc-time]').fill('08:30')
            rev=f.cleaner.summary(OWNER)['settings']['revision'];f.cleaner.update_settings(dict(request_id='browser-other-session',expected_revision=rev,schedule_time='09:00'),OWNER)
            before=f.count('cleaner_requests');page.locator('[data-kc-time-save]').click()
            expect(page.locator('[data-kc-message]')).to_contain_text('Данные уже изменились')
            expect(page.locator('[data-kc-time-edit]')).to_have_text('09:00')
            check('stale_settings_visible_no_mutation',f.count('cleaner_requests')==before)
            page.locator('[data-kc-time-cancel]').click()
            # A decision is saved once even when double-clicked, and becomes history.
            before=f.count('cleaner_manual_overrides');question=page.locator('[data-kc-review]').first
            question.get_by_role('button',name='Оставить',exact=True).dblclick()
            expect(page.locator('[data-kc-pending]')).to_have_text('2')
            check('double_click_decision_once',f.count('cleaner_manual_overrides')==before+1)
            page.locator('[data-kc-history-open]').click();expect(page.locator('[data-kc-history]')).to_contain_text('Решение владельца')
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-page="history"]')).to_be_visible();check('history_reload_deep_link')
            page.locator('[data-kc-page="history"] [data-kc-back]').click()
            # Runs use one persistent slot, buttons do not generate duplicate jobs.
            before=f.count('cleaner_runs');page.locator('[data-kc-run]').dblclick()
            expect(page.locator('[data-kc-status]')).to_have_text('Проверка в очереди')
            check('double_click_run_one_job',f.count('cleaner_runs')==before+1)
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-status]')).to_have_text('Проверка в очереди');check('queued_job_survives_reload')
            page.locator('[data-kc-enabled]').click();expect(page.locator('[data-kc-enabled-label]')).to_have_text('Авточистка выключена');check('disable_saved',not f.request('/summary')[1]['settings']['enabled'])
            # Profile draft and explicit activation are two independently saved commands.
            page.locator('[data-kc-profiles-open]').click();page.locator('[data-kc-profile-number]').fill('102');page.locator('[data-kc-profile-find] button').click()
            expect(page.locator('[data-kc-profile-form]')).to_be_visible();page.locator('[data-kc-profile-models]').select_option(['17 pro']);page.locator('[data-kc-profile-source]').fill('Тест совместимости <img src=x onerror="window.cleanerXss=2">');page.locator('[data-kc-profile-save]').click()
            expect(page.locator('[data-kc-profile-versions]')).to_contain_text('не активна');check('draft_does_not_activate',f.request('/profiles/102')[1]['active_version'] is None)
            page.get_by_role('button',name='Активировать версию',exact=True).click();expect(page.locator('[data-kc-profile-versions]')).to_contain_text('действует');check('explicit_profile_activation',f.request('/profiles/102')[1]['active_version']==1)
            check('profile_source_xss_is_text',page.locator('[data-kc-profile-versions] img').count()==0 and page.evaluate('window.cleanerXss===undefined'))
            page.goto(f.base_url+f.url.split(f.base_url)[1].split('?')[0]+'?tab=ads',wait_until='domcontentloaded');expect(page.locator('[data-ads-panel]')).to_be_visible();expect(page.locator('[data-keyword-cleaner]')).to_be_hidden();check('old_ads_deep_link_opens_bids')
            check('no_browser_js_errors',not errors);page.close()
        # Each variant is rendered by the real app from isolated synthetic SQL state.
        for mode,status in [('empty','Выполнено'),('partial','Выполнено частично'),('failed','Не выполнено'),('unresolved','Проверяем результат WB'),('profile-required','Выполнено частично')]:
            with running_fixture(mode) as f:
                page=browser.new_page(viewport={'width':1280,'height':960})
                if mode=='failed':
                    page.request.post(f.base_url+'/login',form={'username':'owner','password':PASSWORD,'next':f.url.split(f.base_url)[1]})
                    page.goto(f.url,wait_until='domcontentloaded')
                else: browser_login(page,f)
                expect(page.locator('[data-kc-status]')).to_have_text(status)
                if mode in {'partial','failed','unresolved'}:check(mode+'_not_success',page.locator('[data-kc-status]').get_attribute('data-tone')!='success')
                if mode=='empty':expect(page.locator('[data-kc-reviews]')).to_contain_text('Все вопросы разобраны');check('empty_review_state')
                if mode=='failed':check('missing_counts_are_not_zero',page.locator('[data-kc-checked]').inner_text()=='—')
                if mode=='unresolved':
                    expect(page.locator('[data-kc-reviews]')).to_contain_text('Все вопросы разобраны');expect(page.locator('[data-kc-indicator]')).to_be_visible();check('unresolved_remains_when_reviews_empty')
                    with f.cleaner.store.transaction() as c:c.execute("UPDATE cleaner_write_operations SET state='submitted' WHERE operation_id='fixture-unresolved'")
                    page.locator('[data-kc-refresh]').click();expect(page.locator('[data-kc-enabled]')).to_be_enabled();page.locator('[data-kc-enabled]').click();expect(page.locator('[data-kc-enabled-label]')).to_have_text('Останавливается');check('disable_inflight_stopping')
                    with f.cleaner.store.transaction() as c:c.execute("UPDATE cleaner_write_operations SET state='unresolved' WHERE operation_id='fixture-unresolved'")
                    page.locator('[data-kc-refresh]').click();expect(page.locator('[data-kc-enabled-label]')).to_have_text('Авточистка выключена');expect(page.locator('[data-kc-status]')).to_have_text('Проверяем результат WB');check('disabled_unresolved_not_disguised')
                if mode=='profile-required':
                    expect(page.locator('[data-kc-alerts]')).to_contain_text('Нужна настройка товаров');page.locator('[data-kc-profiles-open]').click();expect(page.locator('[data-kc-profiles]')).to_contain_text('Товар WB 102');check('unknown_profile_visible_setting')
                filename=mode+'.png';page.screenshot(path=str(output/filename),full_page=True);screens.append(filename);page.close()
        with running_fixture('empty') as f:
            page=browser.new_page();browser_login(page,f,'reader');expect(page.locator('[data-kc-run]')).to_be_disabled();expect(page.locator('[data-kc-alerts]')).to_contain_text('только назначенному владельцу');check('reader_view_is_read_only')
            before=f.count('cleaner_requests');response=page.request.post(f.base_url+PREFIX+'/runs',data={'request_id':'browser-reader-direct'},headers={'Origin':f.base_url,'Content-Type':'application/json','X-WB-Keyword-Cleaner-CSRF':'1'});check('browser_reader_direct_post_forbidden',response.status==403 and f.count('cleaner_requests')==before);page.close()
        # Preserve an ambiguous command across reload, then recover by GET alone.
        with running_fixture('empty') as f:
            page=browser.new_page();browser_login(page,f);posts=[]
            def pending_post(route):
                posts.append(route.request.post_data_json);assert route.fetch().status==202;route.abort()
            page.route('**/keyword-cleaner/settings',pending_post);page.route('**/keyword-cleaner/requests/*',lambda route:route.abort())
            page.locator('[data-kc-enabled]').click();expect(page.locator('[data-kc-recover]')).to_be_visible();expect(page.locator('[data-kc-message]')).to_contain_text('Не удалось проверить сохранение')
            page.reload(wait_until='domcontentloaded');expect(page.locator('[data-kc-recover]')).to_be_visible();check('uncertain_command_survives_reload')
            page.unroute('**/keyword-cleaner/requests/*');page.locator('[data-kc-recover]').click();expect(page.locator('[data-kc-recover]')).to_be_hidden();check('reloaded_command_recovered_without_new_post',len(posts)==1);page.close()
        # An unresponsive POST connection is aborted after the bounded deadline.
        with running_fixture('empty') as f:
            page=browser.new_page();browser_login(page,f);posts=[]
            def hanging_post(route):
                posts.append(route.request.post_data_json);assert route.fetch().status==202
                # Keep browser response pending; its own deadline initiates recovery.
            page.route('**/keyword-cleaner/settings',hanging_post)
            start=time.monotonic();page.locator('[data-kc-enabled]').click();expect(page.locator('[data-kc-message]')).to_contain_text('Изменения сохранены',timeout=16000)
            check('hanging_post_deadline_recovers_one_request',len(posts)==1 and 9<=time.monotonic()-start<16);page.close()
        browser.close()
    return dict(passed=len(checks),checks=checks,screenshots=[str(output/name) for name in screens],wb_writes=0)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();result=run(args.output);(args.output/'browser-receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
