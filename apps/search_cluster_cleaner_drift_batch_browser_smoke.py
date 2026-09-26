#!/usr/bin/env python3
"""Browser owner continuation of one exact held pair and untouched batch tail."""
from __future__ import annotations
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect,sync_playwright
from apps.search_cluster_cleaner_browser_smoke import browser_login
from apps.search_cluster_cleaner_web_fixture import running_fixture


def main():
    with running_fixture() as fixture,sync_playwright() as playwright:
        with fixture.cleaner.store.transaction() as c:
            c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(fixture.cleaner.key,))
        page=playwright.chromium.launch().new_page(viewport={'width':1440,'height':950})
        state={'phase':'old','posts':[]}
        def summary(route):
            body=route.fetch().json()
            body.update(manual_worker_state='ready',manual_worker_alive=True,last_manual_job=None,
                        last_manual_batch=dict(batch_id='synthetic-drift-batch',state='partial' if state['phase']!='running' else 'running',
                                               created_at='2026-09-26T10:00:00Z',updated_at={'old':'2026-09-26T10:20:00Z','running':'2026-09-26T10:21:00Z','final':'2026-09-26T10:30:00Z'}[state['phase']]))
            route.fulfill(status=200,content_type='application/json',body=json.dumps(body,ensure_ascii=False))
        def status(route):
            phase=state['phase'];rows=[]
            for index in range(26):
                row=dict(index=index,advert_id=100+index,nm_id=101,campaign_name='Кампания '+str(100+index),product_title='Стекло',
                         selected_status='active',state='complete' if index<14 or phase=='final' and index>14 else 'skipped' if index==14 or phase=='old' else 'queued',
                         stage='finished' if index<15 or phase=='final' else 'not_started',job_id='child-'+str(index) if index<=14 or phase=='final' else None,
                         write_run_id=None,new_checked=3 if index==14 else 1 if index<14 or phase=='final' else None,
                         confirmed_excluded=0 if index!=14 and (index<14 or phase=='final') else None,
                         allowed=1 if index==14 else 0 if index<14 or phase=='final' else None,
                         review_count=0 if index<=14 or phase=='final' else None,pending_count=0 if index<=14 or phase=='final' else None,
                         delivery_state='unknown',not_sent_count=None,already_excluded=0 if index<=14 or phase=='final' else None,
                         error_code='external_state_drift' if index==14 else 'not_started_after_failure' if phase=='old' and index>14 else None,
                         error='Список исключений WB изменился; эта пара оставлена на разборе без новой отправки' if index==14 else None,
                         review_required=index==14,drift_queries=[dict(query='аксессуары для iphone 16 pro max',verdict='allow',rule_id='APPROVED_BASELINE',reason='Подходит товару')] if index==14 else [])
                rows.append(row)
            body=dict(batch_id='synthetic-drift-batch',state='partial' if phase!='running' else 'running',stage='finished' if phase!='running' else 'next_target',
                      selected_count=26,done_count=15 if phase=='old' else 26 if phase=='final' else 15,confirmed_count=14 if phase!='final' else 25,completed_count=14 if phase!='final' else 25,
                      no_change_count=0,partial_count=0,failed_count=0,skipped_count=0,
                      not_started_count=11 if phase=='old' else 0 if phase=='final' else 11,held_count=1,
                      can_resume=phase=='old',resume_index=14 if phase=='old' else None,current_index=26 if phase!='running' else 15,
                      current_target=None,created_at='2026-09-26T10:00:00Z',updated_at={'old':'2026-09-26T10:20:00Z','running':'2026-09-26T10:21:00Z','final':'2026-09-26T10:30:00Z'}[phase],
                      items=rows,error_code='scan_partial' if phase=='old' else None,error=None)
            route.fulfill(status=200,content_type='application/json',body=json.dumps(body,ensure_ascii=False))
        def detail(route):
            route.fulfill(status=200,content_type='application/json',body=json.dumps(dict(job=dict(scan_decisions=[dict(query='аксессуары для iphone 16 pro max',verdict='allow',state='external_state_drift',observed_state='active',reason='Подходит товару')],state='failed',write_run_id=None),run=None),ensure_ascii=False))
        def resume(route):
            body=route.request.post_data_json
            state['posts'].append(body)
            assert body.keys()=={'request_id'} and route.request.headers.get('x-wb-keyword-cleaner-csrf')=='1'
            state['phase']='running'
            route.fulfill(status=202,content_type='application/json',body=json.dumps(dict(batch_id='synthetic-drift-batch',state='running',current_index=15,review_required_target='114:101')))
        page.route('**/keyword-cleaner/summary',summary)
        page.route('**/keyword-cleaner/manual-batches/synthetic-drift-batch/items/*',detail)
        page.route('**/keyword-cleaner/manual-batches/synthetic-drift-batch/resume',resume)
        page.route('**/keyword-cleaner/manual-batches/synthetic-drift-batch',status)
        browser_login(page,fixture)
        page.reload(wait_until='domcontentloaded')
        card=page.locator('[data-kc-batch-result]')
        expect(card).to_contain_text('обработано: 15')
        expect(card).to_contain_text('ещё не начато: 11')
        expect(card.get_by_role('button',name='Продолжить остальные пары этой группы')).to_be_enabled()
        page.get_by_role('button',name='Далее').click()
        page.get_by_role('button',name='Подробности').nth(4).click()
        expect(card).to_contain_text('Согласованное решение: оставить')
        expect(card).to_contain_text('Сейчас отсутствует в исключениях WB')
        page.reload(wait_until='domcontentloaded')
        expect(card.get_by_role('button',name='Продолжить остальные пары этой группы')).to_be_enabled()
        card.get_by_role('button',name='Продолжить остальные пары этой группы').click()
        expect(card).not_to_contain_text('Продолжить остальные пары этой группы')
        assert len(state['posts'])==1
        state['phase']='final'
        page.reload(wait_until='domcontentloaded')
        expect(card).to_contain_text('обработано: 26')
        expect(card).to_contain_text('завершено пар: 25')
        expect(card).to_contain_text('на разборе: 1')
        expect(card).to_contain_text('ещё не начато: 0')
        assert len(state['posts'])==1
        page.context.browser.close()
    print('search_cluster_cleaner_drift_batch_browser_smoke: PASS')

if __name__=='__main__':main()
