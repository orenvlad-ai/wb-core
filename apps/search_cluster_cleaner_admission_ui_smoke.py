#!/usr/bin/env python3
"""Local admission/receipt and real browser regressions; no WB traffic."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright

from apps.registry_upload_http_entrypoint_live import CleanerWorkerSupervisor
from apps.search_cluster_cleaner_web_fixture import OWNER, PASSWORD, running_fixture
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.contracts.search_cluster_cleaner import CleanerError, Target


def disabled(fixture):
    with fixture.cleaner.store.transaction() as c:
        c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',
                  (fixture.cleaner.key,))


def batch_payload(request_id):
    return dict(request_id=request_id,selected_categories=['active'],targets=[dict(advert_id=10101,nm_id=101)])


class Catalog:
    monotonic = staticmethod(time.monotonic)

    def count_statuses(self, deadline):
        return {10101: 9}

    def _adverts(self, ids, deadline):
        assert ids == [10101]
        return [Target(10101,101,name='Тестовая кампания 10101',status=9,contract_verified=True)]


def backend():
    with running_fixture() as f:
        disabled(f)
        f.web.worker_status=lambda:'busy'
        f.web.worker_alive=None
        status, rejected, _=f.request('/manual-batches',batch_payload('busy-no-heartbeat-0001'))
        assert status==503 and rejected['definitively_not_accepted'] is True
        f.web.worker_alive=lambda:True
        assert f.request('/summary')[1]['manual_queue_busy'] is False
        with patch.object(CleanerWbSource,'from_env',return_value=Catalog()):
            status, accepted, _=f.request('/manual-batches',batch_payload('busy-idle-batch-0001'))
            assert status==202,accepted
            assert f.request('/summary')[1]['manual_queue_busy'] is True
            status, rejected, _=f.request('/manual-batches',batch_payload('busy-active-batch-0002'))
            assert status==409 and rejected['code']=='manual_batch_active',(status,rejected)
            status, repeat, _=f.request('/manual-batches',batch_payload('busy-idle-batch-0001'))
            assert status==202 and repeat==accepted
        assert f.count('cleaner_requests')==3 # initial scan, settings, accepted batch
    with running_fixture() as f:
        disabled(f)
        f.web.worker_status=lambda:'busy'
        f.web.worker_alive=lambda:True
        status, accepted, _=f.request('/manual-clean',dict(request_id='busy-idle-single-0001',advert_id=10101,nm_id=101))
        assert status==202,accepted
        status, rejected, _=f.request('/manual-clean',dict(request_id='busy-active-single-0002',advert_id=10101,nm_id=101))
        assert status==409 and rejected['code'] in {'manual_job_active','manual_queue_blocked'},(status,rejected)
        assert f.request('/summary')[1]['manual_queue_busy'] is True
    with running_fixture() as f:
        disabled(f)
        f.web.worker_status=lambda:'busy'
        f.web.worker_alive=lambda:True
        gate=threading.Barrier(2)
        def start(which):
            gate.wait()
            try:
                if which=='batch':return f.web.start_manual_batch(batch_payload('race-batch-0001'),OWNER)
                return f.web.start_manual_clean(dict(request_id='race-single-0001',advert_id=10101,nm_id=101),OWNER)
            except CleanerError as error:return error.code
        with patch.object(CleanerWbSource,'from_env',return_value=Catalog()),ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(start,['batch','single']))
        assert sum(isinstance(result,dict) for result in results)==1,results
        assert any(result in {'manual_queue_blocked','manual_batch_active','manual_job_active'} for result in results if isinstance(result,str)),results
        assert f.request('/summary')[1]['manual_queue_busy'] is True
    with running_fixture() as f:
        disabled(f)
        f.web.worker_status=lambda:'starting'
        f.web.worker_alive=lambda:True
        for path,payload in [('/manual-batches',batch_payload('definite-batch-0001')),
                             ('/manual-clean',dict(request_id='definite-single-0001',advert_id=10101,nm_id=101))]:
            status, rejected, _=f.request(path,payload)
            assert status==503 and rejected['code']=='manual_worker_unavailable',rejected
            assert rejected['definitively_not_accepted'] is True and rejected['request_id']==payload['request_id']
            assert f.request('/requests/'+payload['request_id'])[0]==404
        assert f.request('/requests/not-found-0001')[0]==404
        status, rejected, _=f.request('/manual-batches',batch_payload('csrf-denied-0001'),headers={'Origin':'https://invalid.example'})
        assert status==403 and not rejected.get('definitively_not_accepted')
    supervisor=CleanerWorkerSupervisor(Path('/unused'))
    supervisor.process=SimpleNamespace(pid=4288,poll=lambda:None)
    for age,expected in [(0,'busy'),(11,'starting')]:
        health=json.dumps(dict(pid=4288,state='busy',updated_at=time.time()-age))
        with patch.object(Path,'read_text',return_value=health):
            assert supervisor.status()==expected,(age,supervisor.status())


def login(page,f):
    response=page.request.post(f.base_url+'/login',form={'username':'owner','password':PASSWORD,
                               'next':f.url.split(f.base_url)[1]})
    assert response.status==200
    page.goto(f.url,wait_until='domcontentloaded')
    expect(page.locator('[data-keyword-cleaner]')).to_be_visible()


def start_batch(page):
    page.locator('[data-kc-batch-open]').click()
    expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
    page.locator('[data-kc-batch-start]').click()


def browser():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        with running_fixture(scenario='admission-ui') as f:
            page=browser.new_page()
            login(page,f)
            before=f.count('cleaner_requests')
            expect(page.locator('[data-kc-enabled-label]')).to_have_text('Запуск вручную')
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Массовая чистка · Выполнено')
            start_batch(page)
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Предыдущая массовая чистка')
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('Запуск не принят',timeout=15000)
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Предыдущая массовая чистка')
            assert len(f.admission_posts)==3 and len(set(f.admission_posts))==1,f.admission_posts
            assert f.request('/requests/'+f.admission_posts[0])[0]==404
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Предыдущая массовая чистка')
            start_batch(page)
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Массовая чистка · Выполнено',timeout=10000)
            assert len(f.admission_posts)==4 and f.admission_posts[3]!=f.admission_posts[0]
            assert f.count('cleaner_requests')==before+1
            page.close()
        with running_fixture(scenario='admission-ui') as f:
            page=browser.new_page()
            login(page,f)
            posts=[]
            def unknown(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=503,content_type='application/json',body=json.dumps(dict(code='storage_unavailable',error='unknown')))
            page.route('**/keyword-cleaner/manual-batches',unknown)
            start_batch(page)
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('не подтверждён',timeout=20000)
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Предыдущая массовая чистка')
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('не подтверждён')
            assert len(posts)==1 and f.admission_posts==[]
            page.close()
        with running_fixture(scenario='admission-ui') as f:
            page=browser.new_page()
            login(page,f)
            page.locator('[data-kc-manual-advert]').select_option('10101')
            page.locator('[data-kc-manual-nm]').select_option('101')
            posts=[]
            def unknown_single(route):
                posts.append(route.request.post_data_json)
                route.fulfill(status=503,content_type='application/json',body=json.dumps(dict(code='storage_unavailable',error='unknown')))
            page.route('**/keyword-cleaner/manual-clean',unknown_single)
            page.locator('[data-kc-run]').click()
            expect(page.locator('[data-kc-recover]')).to_be_visible()
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Предыдущая массовая чистка')
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('[data-kc-recover]')).to_be_visible()
            expect(page.locator('[data-kc-batch-result]')).to_contain_text('Предыдущая массовая чистка')
            assert len(posts)==1
            page.close()
        with running_fixture(scenario='admission-ui') as f:
            page=browser.new_page()
            login(page,f)
            page.locator('[data-kc-manual-advert]').select_option('10101')
            page.locator('[data-kc-manual-nm]').select_option('101')
            posts=[]
            before=f.count('cleaner_requests')
            def definite_single(route):
                request=route.request.post_data_json
                posts.append(request)
                route.fulfill(status=503,content_type='application/json',body=json.dumps(dict(
                    code='manual_worker_unavailable',error='Синтетический отказ до приёма',
                    request_id=request['request_id'],definitively_not_accepted=True)))
            page.route('**/keyword-cleaner/manual-clean',definite_single)
            page.locator('[data-kc-run]').click()
            expect(page.locator('[data-kc-message]')).to_contain_text('Запуск не принят')
            expect(page.locator('[data-kc-recover]')).to_be_hidden()
            assert len(posts)==1 and f.request('/requests/'+posts[0]['request_id'])[0]==404
            assert f.count('cleaner_requests')==before
            page.unroute('**/keyword-cleaner/manual-clean',definite_single)
            expect(page.locator('[data-kc-run]')).to_be_enabled()
            page.locator('[data-kc-run]').click()
            expect(page.locator('[data-kc-manual-stage]')).to_be_visible()
            for _ in range(50):
                if f.count('cleaner_requests')==before+1:break
                time.sleep(0.1)
            assert f.count('cleaner_requests')==before+1
            page.close()
        with running_fixture(scenario='admission-ui') as f:
            page=browser.new_page()
            login(page,f)
            start_batch(page)
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('Запуск не принят',timeout=15000)
            page.locator('[data-kc-manual-advert]').select_option('10101')
            page.locator('[data-kc-manual-nm]').select_option('101')
            page.locator('[data-kc-run]').click()
            expect(page.locator('[data-kc-manual-stage]')).to_be_visible()
            expect(page.locator('[data-kc-batch-stage]')).to_be_hidden()
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('[data-kc-batch-stage]')).to_be_hidden()
            page.close()
        with running_fixture(scenario='admission-ui') as f:
            page=browser.new_page()
            def prior_single(route):
                summary=route.fetch().json()
                summary['last_manual_job']=dict(job_id='synthetic-prior-single',advert_id=10101,nm_id=101,
                    state='no_change',stage='finished',created_at='2026-09-12T01:59:00Z',updated_at='2026-09-12T02:00:00Z')
                route.fulfill(status=200,content_type='application/json',body=json.dumps(summary))
            page.route('**/keyword-cleaner/summary',prior_single)
            login(page,f)
            expect(page.locator('[data-kc-manual-result]')).to_contain_text('Новых изменений нет')
            start_batch(page)
            expect(page.locator('[data-kc-manual-result]')).to_contain_text('Предыдущая ручная чистка')
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('Запуск не принят',timeout=15000)
            expect(page.locator('[data-kc-manual-result]')).to_contain_text('Предыдущая ручная чистка')
            page.close()
        browser.close()


if __name__=='__main__':
    backend();browser();print('search cluster cleaner admission UI smoke: ok')
