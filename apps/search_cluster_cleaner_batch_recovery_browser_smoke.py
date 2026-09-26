#!/usr/bin/env python3
"""Local browser checks for one durable CPM batch intent and signed owner access."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright

from apps.search_cluster_cleaner_web_fixture import PASSWORD, running_fixture
from packages.adapters.search_cluster_cleaner_http import PREFIX
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.contracts.search_cluster_cleaner import Target


def reply(route, status, body):
    route.fulfill(status=status, content_type='application/json', body=json.dumps(body, ensure_ascii=False))


def browser_login(page, fixture):
    response = page.request.post(fixture.base_url + '/login', form={
        'username': 'owner', 'password': PASSWORD,
        'next': fixture.url.split(fixture.base_url)[1],
    })
    assert response.status == 200
    page.goto(fixture.url, wait_until='domcontentloaded')
    expect(page.locator('[data-keyword-cleaner]')).to_be_visible()
    expect(page.locator('[data-kc-status]')).not_to_have_text('Загружаем…')


def eligibility(page):
    item = dict(advert_id=10101, nm_id=101, campaign_name='Тестовая CPM-кампания',
                product_title='Стекло iPhone 16 Pro Max', status='active', status_code=9,
                payment_type='cpm', eligible=True, admitted=True, profile_ready=True, reason=None)
    data = dict(items=[item], loading=False, error=None,
                counts=dict(total=1, eligible=1, selectable_active=1, selectable_paused=0,
                            profile_required=0, ineligible=0, unknown=0),
                categories={status: dict(selectable=status in ('active', 'paused'), reason=None)
                            for status in ('active', 'paused', 'completed', 'archive')})
    page.route('**/keyword-cleaner/manual-batches/eligibility*', lambda route: reply(route, 200, data))


def click_start(page):
    page.locator('[data-kc-batch-open]').click()
    expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
    page.locator('[data-kc-batch-start]').click()


def manual_ready(fixture):
    with fixture.cleaner.store.transaction() as connection:
        connection.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',
                           (fixture.cleaner.key,))


def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # CPM counts and selection ignore CPC even if a malformed projection
        # happens to contain a CPC row. The button is visible on a narrow screen.
        with running_fixture() as fixture:
            manual_ready(fixture)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            rows = [
                dict(advert_id=201, nm_id=101, campaign_name='CPM активная', product_title='Стекло', status='active', payment_type='cpm', eligible=True, reason=None),
                dict(advert_id=202, nm_id=101, campaign_name='CPM нужна настройка', product_title='Стекло', status='active', payment_type='cpm', eligible=False, reason='profile_required'),
                dict(advert_id=203, nm_id=101, campaign_name='CPM пауза', product_title='Стекло', status='paused', payment_type='cpm', eligible=True, reason=None),
                dict(advert_id=204, nm_id=101, campaign_name='CPM архив', product_title='Стекло', status='archive', payment_type='cpm', eligible=False, reason='unsupported_campaign_status'),
                dict(advert_id=205, nm_id=101, campaign_name='CPC чужая', product_title='Стекло', status='active', payment_type='cpc', eligible=True, reason=None),
            ]
            data = dict(items=rows, loading=False, error=None,
                        counts=dict(total=4, eligible=2, selectable_active=1, selectable_paused=1,
                                    profile_required=1, ineligible=1, unknown=None),
                        categories={status: dict(selectable=status in ('active', 'paused'), reason=None)
                                    for status in ('active', 'paused', 'completed', 'archive')})
            page.route('**/keyword-cleaner/manual-batches/eligibility*', lambda route: reply(route, 200, data))
            browser_login(page, fixture)
            before = fixture.count('cleaner_requests')
            page.locator('[data-kc-batch-open]').click()
            expect(page.locator('[data-kc-batch-overview]')).to_have_text('CPM-пары: получено 4 · готовы 2 · требуют настройки 1 · без подтверждения —')
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Активные · всего 2')
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Готовы 1 · выбрано 1 · требуют настройки 1')
            expect(page.locator('[data-kc-batch-choices] input[value="205:101"]')).to_have_count(0)
            expect(page.locator('[data-kc-batch-choices] input[value="204:101"]')).to_have_count(0)
            expect(page.locator('[data-kc-batch-start]')).to_be_enabled()
            page.locator('[data-kc-batch-choices] input[value="203:101"]').check()
            expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано пар: 2')
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Приостановленные · всего 1')
            expect(page.locator('[data-kc-batch-categories]')).to_contain_text('Готовы 1 · выбрано 1')
            assert fixture.count('cleaner_requests') == before
            page.screenshot(path=str(output/'cpm-modal-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(output/'cpm-modal-mobile.png'))
            dialog = page.locator('[data-kc-batch-dialog]').bounding_box()
            button = page.locator('[data-kc-batch-start]').bounding_box()
            assert dialog and button and button['y'] + button['height'] <= min(844, dialog['y'] + dialog['height']) + 1
            checks.append('cpm_only_counts_selection_and_mobile_primary_visible_without_submit')
            page.close()

        # A proven rollback allows only the identical payload and request ID to be retried.
        with running_fixture() as fixture:
            manual_ready(fixture)
            page = browser.new_page()
            eligibility(page)
            posts, reads = [], []

            def post(route):
                posts.append(route.request.post_data_json)
                if len(posts) == 1:
                    reply(route, 503, dict(code='storage_rolled_back',
                                           definitively_not_accepted=True,
                                           request_id=posts[0]['request_id'], retry_after_ms=1000))
                else:
                    reply(route, 202, dict(batch_id='rollback-recovered', state='queued', selected_count=1))

            def read(route):
                reads.append(route.request.url.rsplit('/', 1)[-1])
                reply(route, 404, dict(error='not_found'))

            page.route('**/keyword-cleaner/manual-batches', post)
            page.route('**/keyword-cleaner/requests/*', read)
            browser_login(page, fixture)
            before = fixture.count('cleaner_requests')
            click_start(page)
            expect(page.locator('[data-kc-message]')).to_contain_text('Массовая чистка принята', timeout=8000)
            expect(page.locator('[data-kc-recover]')).to_be_hidden(timeout=8000)
            assert len(posts) == 2 and posts[0] == posts[1]
            assert reads == [posts[0]['request_id']]
            assert fixture.count('cleaner_requests') == before
            checks.append('proven_rollback_retries_identical_intent_after_404')
            page.close()

        # A 500 followed by any number of 404s is uncertain, even after reload.
        with running_fixture() as fixture:
            manual_ready(fixture)
            page = browser.new_page()
            eligibility(page)
            posts, reads = [], []
            saved = {'available': False}

            def post(route):
                posts.append(route.request.post_data_json)
                reply(route, 500, dict(error='uncertain'))

            def read(route):
                reads.append(route.request.url.rsplit('/', 1)[-1])
                if saved['available']:
                    reply(route, 200, dict(batch_id='late-accepted', state='queued', selected_count=1))
                else:
                    reply(route, 404, dict(error='not_found'))

            page.route('**/keyword-cleaner/manual-batches', post)
            page.route('**/keyword-cleaner/requests/*', read)
            browser_login(page, fixture)
            before = fixture.count('cleaner_requests')
            click_start(page)
            expect(page.locator('[data-kc-recover]')).to_be_visible(timeout=18000)
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('не подтверждён')
            expect(page.locator('[data-kc-batch-recover]')).to_be_visible()
            expect(page.locator('[data-kc-batch-recover]')).to_be_enabled()
            page.screenshot(path=str(output/'batch-pending.png'), full_page=True)
            assert len(posts) == 1 and reads and all(value == posts[0]['request_id'] for value in reads)
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('[data-kc-recover]')).to_be_visible()
            saved['available'] = True
            page.locator('[data-kc-batch-recover]').click()
            expect(page.locator('[data-kc-recover]')).to_be_hidden()
            expect(page.locator('[data-kc-batch-recover]')).to_be_hidden()
            assert len(posts) == 1 and fixture.count('cleaner_requests') == before
            checks.append('unknown_500_and_404_never_repost_reload_recovers_same_id')
            page.close()

        # Definitive validation failure clears the pending command and says so.
        with running_fixture() as fixture:
            manual_ready(fixture)
            page = browser.new_page()
            eligibility(page)
            posts = []

            def rejected(route):
                posts.append(route.request.post_data_json)
                reply(route, 422, dict(code='invalid_target', error='Пара больше недоступна'))

            page.route('**/keyword-cleaner/manual-batches', rejected)
            browser_login(page, fixture)
            click_start(page)
            expect(page.locator('[data-kc-batch-stage-text]')).to_contain_text('Запуск не принят')
            expect(page.locator('[data-kc-recover]')).to_be_hidden()
            assert len(posts) == 1
            checks.append('validation_rejection_is_not_accepted')
            page.close()

        # The real local HTTP handler derives site-owner authority from a signed
        # bootstrap session. No body field can grant the same authority to admin.
        with running_fixture(cleaner_owner_username='codex') as fixture:
            manual_ready(fixture)
            page = browser.new_page()
            browser_login(page, fixture)
            assert fixture.cleaner.owner_username == 'codex'
            before = fixture.count('cleaner_requests')

            class ReadOnlyCatalog:
                @staticmethod
                def monotonic():
                    return 0.0

                @staticmethod
                def count_statuses(deadline):
                    return {10101: 9}

                @staticmethod
                def _adverts(ids, deadline):
                    assert ids == [10101]
                    return [Target(10101, 101, payment_type='cpm', bid_type='manual',
                                   status=9, name='Тестовая кампания 10101', contract_verified=True)]

            admin = fixture.login('admin')
            status, _, _ = fixture.request('/manual-batches',
                                           dict(request_id='runtime-admin-denied', selected_categories=['active'],
                                                targets=[dict(advert_id=10101, nm_id=101)]), opener=admin)
            assert status == 403 and fixture.count('cleaner_requests') == before
            with patch.object(CleanerWbSource, 'from_env', return_value=ReadOnlyCatalog()):
                click_start(page)
                expect(page.locator('[data-kc-message]')).to_contain_text('Массовая чистка принята', timeout=8000)
                expect(page.locator('[data-kc-recover]')).to_be_hidden(timeout=8000)
            assert fixture.count('cleaner_requests') == before + 1
            with fixture.cleaner.store.read() as connection:
                row = connection.execute("SELECT actor,request_id FROM cleaner_requests WHERE request_id NOT LIKE 'fixture-%' ORDER BY rowid DESC LIMIT 1").fetchone()
            assert row and row['actor'] == 'owner'
            status, outcome, _ = fixture.request('/requests/' + row['request_id'])
            assert status == 200 and outcome.get('batch_id')
            checks.append('signed_bootstrap_owner_can_start_exact_cpm_batch_runtime_admin_cannot')
            page.close()

        browser.close()
    result = dict(passed=len(checks), checks=checks, wb_writes=0)
    (output/'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


if __name__ == '__main__':
    location = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='cleaner-batch-recovery-'))
    print(json.dumps(run(location), ensure_ascii=False, indent=2))
