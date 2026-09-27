#!/usr/bin/env python3
"""Owner click through real batch worker and loopback FakeWB readback."""
from __future__ import annotations
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect,sync_playwright
from apps.search_cluster_cleaner_reconcile_integration_fixture import LEGACY_QUERY,running_integration_fixture
from apps.search_cluster_cleaner_web_fixture import PASSWORD


def main():
    with running_integration_fixture() as (fixture,fake,errors),sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1050},accept_downloads=True)
        page_errors=[]
        page.on('pageerror',lambda error:page_errors.append(str(error)))
        response=page.request.post(fixture.base_url+'/login',form=dict(username='owner',password=PASSWORD,next=fixture.url.split(fixture.base_url)[1]))
        assert response.status==200
        page.goto(fixture.url,wait_until='domcontentloaded')
        expect(page.locator('[data-keyword-cleaner]')).to_be_visible()
        expect(page.locator('[data-kc-status]')).not_to_have_text('Загружаем…')
        assert 'Расписание выключено' in page.locator('[data-keyword-cleaner]').inner_text()
        expect(page.locator('[data-kc-reviews]')).to_contain_text(LEGACY_QUERY)
        expect(page.locator('[data-kc-reviews]')).to_contain_text('Старый вопрос владельцу')
        assert fake.writes==[]
        with fixture.cleaner.store.read() as db:
            assert db.execute('SELECT reason FROM cleaner_target_holds WHERE target=?',('11:101',)).fetchone()[0]=='external_state_drift'
        page.locator('[data-kc-batch-open]').click()
        expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано доступных пар: 1',timeout=12000)
        expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
        page.locator('[data-kc-batch-start]').click()
        expect(page.locator('[data-kc-batch-stage]')).to_be_visible()
        card=page.locator('[data-kc-batch-result]')
        try:expect(card).to_contain_text('Массовая чистка · Выполнено',timeout=15000)
        except AssertionError:
            print('WORKER_ERRORS',errors,flush=True)
            print('FAKE_CALLS',fake.calls[-8:],flush=True)
            print('UI_MESSAGE',page.locator('[data-kc-message]').inner_text(),flush=True)
            raise
        assert len(fake.writes)==1,fake.writes
        with fixture.cleaner.store.read() as db:
            assert not db.execute('SELECT 1 FROM cleaner_target_holds WHERE target=?',('11:101',)).fetchone()
            assert db.execute("SELECT 1 FROM cleaner_events WHERE kind='external_drift_reconciled'").fetchone()
        assert 'OLD' not in fake.targets[11]['minus'],fake.targets[11]['minus']
        assert 'стекло iphone 15 pro max' in fake.targets[11]['minus'],fake.targets[11]['minus']
        expect(card).to_contain_text('обработано: 1')
        expect(page.locator('[data-kc-reviews]')).not_to_contain_text(LEGACY_QUERY,timeout=10000)
        expect(page.locator('[data-kc-alerts]')).to_contain_text('WB подтвердил возврат ключей из исключений: 1')
        first_batch=fixture.request('/summary')[1]['last_manual_batch']['batch_id']
        first_item=fixture.request('/manual-batches/'+first_batch)[1]['items'][0]
        assert first_item['confirmed_excluded']>=1 and first_item['returned']>=1 and first_item['controversial']>=1,first_item
        assert first_item['pending_count']==0 and first_item['deferred_count']==0,first_item
        print('FIRST_ITEM_COUNTS',json.dumps({key:first_item[key] for key in ('checked_total','confirmed_excluded','returned','unchanged','controversial','deferred_count','pending_count')},ensure_ascii=False),flush=True)
        card.get_by_role('button',name='Подробности').click()
        expect(card).to_contain_text('Возврат подтверждён')
        expect(card).to_contain_text('Исключение подтверждено')
        expect(card).to_contain_text('OLD')
        expect(card).to_contain_text(LEGACY_QUERY)
        expect(card).to_contain_text('Спорное — оставить')
        expect(card).to_contain_text('Решено автоматически: оставить; внесено в реестр спорных')
        assert 'allowed' not in card.inner_text()
        page.reload(wait_until='domcontentloaded')
        expect(card).to_contain_text('Массовая чистка · Выполнено',timeout=12000)
        expect(page.locator('[data-kc-reviews]')).not_to_contain_text(LEGACY_QUERY)
        assert fixture.request('/reviews')[1]['items']==[]
        with page.expect_download() as download_info:
            page.locator('[data-kc-controversial-export]').click()
        registry=Path(download_info.value.path()).read_bytes()
        assert registry.startswith(b'\xef\xbb\xbf') and LEGACY_QUERY in registry.decode('utf-8-sig')
        assert len(fake.writes)==1
        page.locator('[data-kc-batch-open]').click()
        expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
        page.locator('[data-kc-batch-start]').click()
        expect(card).to_contain_text('без новых изменений: 1',timeout=30000)
        assert len(fake.writes)==1
        second_batch=fixture.request('/summary')[1]['last_manual_batch']['batch_id']
        assert second_batch!=first_batch
        second_item=fixture.request('/manual-batches/'+second_batch)[1]['items'][0]
        assert second_item['state']=='no_change' and second_item['confirmed_excluded']==0 and second_item['returned']==0,second_item
        page.set_viewport_size({'width':1280,'height':900})
        table=page.locator('[data-kc-batch-result] .kc-result-table')
        assert table.locator('thead th').count()==table.locator('tbody > tr').first.locator('td').count()==9
        status=table.locator('.kc-batch-status').first
        expect(status).to_have_text('Без изменений')
        assert status.evaluate('(node)=>{const css=getComputedStyle(node);return node.getBoundingClientRect().width>=110&&css.overflowWrap==="normal"&&css.wordBreak==="normal"}')
        assert page.locator('[data-keyword-cleaner]').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
        page.reload(wait_until='domcontentloaded')
        expect(card).to_contain_text('без новых изменений: 1',timeout=12000)
        assert len(fake.writes)==1
        # A return-only late receipt must be visible in summary, history and
        # run detail even when all exclusion counters are zero.
        def return_only_summary(route):
            payload=route.fetch().json()
            payload['confirmed']=dict(automatic=0,manual=0,pilot=0,returned=1,late_automatic=0,late_manual=0,late_pilot=0,late_returned=1)
            route.fulfill(status=200,content_type='application/json',body=json.dumps(payload,ensure_ascii=False))
        history_items=[
            dict(kind='late_confirmation',created_at='2026-09-26T21:00:00Z',run_id='late-return-only',facts=dict(confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=0,returned=1)),
            dict(kind='owner_decision',created_at='2026-09-26T20:00:00Z',facts=dict(decision='exclude',already_excluded=['OLD'])),
            dict(kind='candidate_prepared',created_at='2026-09-26T19:00:00Z',facts={}),
            dict(kind='write_response',created_at='2026-09-26T18:00:00Z',facts=dict(outcome='validation_rejected')),
            dict(kind='write_rejected',created_at='2026-09-26T17:00:00Z',facts={}),
        ]
        page.route('**/keyword-cleaner/summary',return_only_summary)
        page.route('**/keyword-cleaner/history*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(items=history_items,next_cursor=None),ensure_ascii=False)))
        page.route('**/keyword-cleaner/runs/late-return-only',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(state='complete',effective_state='complete',settlement=dict(confirmed_automatic=0,confirmed_manual=0,confirmed_pilot=0,returned=1),targets=[]),ensure_ascii=False)))
        page.reload(wait_until='domcontentloaded')
        expect(page.locator('[data-kc-alerts]')).to_contain_text('WB подтвердил возврат ключей из исключений: 1')
        expect(page.locator('[data-kc-alerts]')).to_contain_text('возвращено 1')
        page.locator('[data-kc-history-open]').click()
        history=page.locator('[data-kc-history]')
        expect(history).to_contain_text('возвращено из исключений: 1')
        expect(history).to_contain_text('Ключ уже находился в исключениях WB на момент решения')
        expect(history).to_contain_text('Подготовлено изменение списка исключений')
        expect(history).to_contain_text('WB отклонил изменение списка исключений')
        expect(history).not_to_contain_text('Уже исключённые ключи не возвращались')
        history.get_by_role('button',name='Результат').click()
        expect(page.locator('[data-kc-run-detail]')).to_contain_text('возвращено из исключений 1')
        assert len(fake.writes)==1
        assert not errors,errors
        assert not page_errors,page_errors
        browser.close()
    print('search_cluster_cleaner_reconcile_integration_browser_smoke: PASS')


if __name__=='__main__':main()
