#!/usr/bin/env python3
"""Browser proof of synthetic full-set reconciliation over authenticated routes."""
from __future__ import annotations
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect,sync_playwright
from apps.search_cluster_cleaner_browser_smoke import browser_login
from apps.search_cluster_cleaner_web_fixture import running_fixture
from apps.search_cluster_cleaner_reconcile_web_fixture import running_reconcile_fixture


def main():
    with running_reconcile_fixture() as (fixture,scenario),sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1050},accept_downloads=True)
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        browser_login(page,fixture)
        assert 'Расписание выключено' in page.locator('[data-keyword-cleaner]').inner_text()
        assert scenario.posts==[]
        reader=fixture.login('reader')
        assert fixture.request('/controversial.csv',opener=reader)[0]==403
        page.locator('[data-kc-batch-open]').click()
        expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано доступных пар: 1')
        selection_key=page.evaluate("Object.keys(sessionStorage).find(key=>key.startsWith('wb-keyword-cleaner-batch-selection:'))")
        assert selection_key
        page.evaluate("([key,value])=>sessionStorage.setItem(key,JSON.stringify(value))",[selection_key,['10101:101','10104:104','88888:888']])
        page.reload(wait_until='domcontentloaded')
        page.locator('[data-kc-batch-open]').click()
        choices=page.locator('[data-kc-batch-choices]')
        expect(choices).to_contain_text('Из выбора сняты недоступные пары')
        expect(choices).to_contain_text('10104:104')
        expect(choices).to_contain_text('88888:888')
        expect(choices.locator('input[value="10104:104"]')).to_be_disabled()
        expect(choices.locator('input[value="10104:104"]')).not_to_be_checked()
        expect(choices.locator('input[value="10101:101"]')).to_be_checked()
        expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано доступных пар: 1')
        expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
        choices.locator('input[value="10102:102"]').check()
        expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано доступных пар: 2')
        page.locator('[data-kc-batch-start]').click()
        expect(page.locator('[data-kc-batch-stage]')).to_be_visible()
        expect(page.locator('[data-kc-batch-spinner]')).to_be_visible()
        card=page.locator('[data-kc-batch-result]')
        expect(card).to_contain_text('Массовая чистка · Частично',timeout=15000)
        expect(card).to_contain_text('обработано: 2')
        expect(card).to_contain_text('Нет точной статистики WB')
        assert len(scenario.posts)==1
        assert scenario.posts[0]['selected_categories']==['active','paused']
        assert scenario.posts[0]['targets']==[dict(advert_id=10101,nm_id=101),dict(advert_id=10102,nm_id=102)]
        assert scenario.posts[0]['request_id']
        card.get_by_role('button',name='Подробности').first.click()
        expect(card).to_contain_text('старый исключённый одобренный ключ')
        expect(card).to_contain_text('Возврат подтверждён')
        expect(card).to_contain_text('новая чужая модель iphone 17')
        expect(card).to_contain_text('Исключение подтверждено')
        expect(card).to_contain_text('неоднозначное стекло promax')
        expect(card).to_contain_text('Спорное — оставить')
        for heading in ('Было WB','По правилу','Сейчас WB'):
            expect(card).to_contain_text(heading)
        card.get_by_role('button',name='Подробности').first.click()
        expect(card).to_contain_text('ключ только в списке WB')
        expect(card).to_contain_text('Нет точной статистики WB: изменение отложено')
        page.reload(wait_until='domcontentloaded')
        expect(card).to_contain_text('Массовая чистка · Частично',timeout=10000)
        assert len(scenario.posts)==1
        with page.expect_download() as download_info:
            page.locator('[data-kc-controversial-export]').click()
        download=download_info.value
        content=Path(download.path()).read_bytes()
        assert content.startswith(b'\xef\xbb\xbf')
        assert 'неоднозначное стекло promax' in content.decode('utf-8-sig')
        assert len(scenario.posts)==1
        page.locator('[data-kc-batch-open]').click()
        expect(choices.locator('input[value="10101:101"]')).to_be_checked()
        expect(choices.locator('input[value="10102:102"]')).to_be_checked()
        page.locator('[data-kc-batch-start]').click()
        expect(card).to_contain_text('Массовая чистка · Выполнено',timeout=15000)
        expect(card).to_contain_text('без новых изменений: 2')
        expect(card).to_contain_text('Без изменений')
        assert len(scenario.posts)==2
        assert scenario.posts[0]['request_id']!=scenario.posts[1]['request_id']
        page.reload(wait_until='domcontentloaded')
        expect(card).to_contain_text('без новых изменений: 2',timeout=10000)
        assert len(scenario.posts)==2
        def old_registry(route):
            payload=route.fetch().json()
            payload['registry_ready']=False
            route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
        page.route('**/keyword-cleaner/summary',old_registry)
        page.reload(wait_until='domcontentloaded')
        expect(page.locator('[data-kc-alerts]')).to_contain_text('Требуется техническое обновление реестра')
        page.locator('[data-kc-batch-open]').click()
        expect(page.locator('[data-kc-batch-start]')).to_be_disabled()
        assert len(scenario.posts)==2
        assert not errors,errors
        with running_fixture() as reset_fixture:
            stale=browser.new_page()
            browser_login(stale,reset_fixture)
            scope=reset_fixture.request('/summary')[1]['configuration']['command_scope']
            stale.evaluate("scope=>{sessionStorage.setItem('wb-keyword-cleaner-manual-job:'+scope,JSON.stringify({job_id:'previous-fixture-job',started_at:Date.now()}));sessionStorage.setItem('wb-keyword-cleaner-batch-job:'+scope,JSON.stringify({batch_id:'previous-fixture-batch',started_at:Date.now()}));}",scope)
            stale.reload(wait_until='domcontentloaded')
            expect(stale.locator('[data-kc-selected-target]')).to_be_hidden()
            assert 'undefined' not in stale.locator('[data-keyword-cleaner]').inner_text()
            stale.close()
        browser.close()
    print('search_cluster_cleaner_reconcile_browser_smoke: PASS')


if __name__=='__main__':main()
