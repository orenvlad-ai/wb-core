#!/usr/bin/env python3
"""Browser -> authenticated local HTTP -> durable batch/child -> loopback WB.

The paused child is deliberately persisted at the write readback boundary.
Production-shaped SQLite fault injection is covered by batch_smoke; this file
checks that the owner can resume the same saved child from the browser.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright

from apps import search_cluster_cleaner_stage_e as stage_e
from apps import search_cluster_cleaner_stage_e_recovery_smoke as recovery_smoke
from packages.application import search_cluster_cleaner_self_service as self_service
from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_write_fixture import Clock, FakeWB
from apps.sheet_vitrina_v1_ads_smoke import FakePromotionSource, NOW, _build_ads_block, _seed_runtime
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH, build_registry_upload_http_server,
)
from packages.adapters.search_cluster_cleaner_wb import AccountLimiter, CleanerWbSource
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.registry_upload_http_entrypoint import CHANGE_REGISTRY_ACCOUNT_SCOPE
from packages.application.search_cluster_cleaner import batch_child_id
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator, batch_status
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter, ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_store import CleanerTransactionRolledBack
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.search_cluster_cleaner import Account, Principal, Target


PASSWORD = 'synthetic-browser-only'
IDS = tuple(range(11, 37))


def _card_package(box: Sandbox) -> dict:
    characteristics = [dict(id=1, name='Модель', value=['iPhone 16 Pro Max']),
                       dict(id=2, name='Тип', value=['Обычное стекло'])]
    card = dict(nm_id='101', title='Synthetic glass', vendor_code='synthetic',
                description='Approved synthetic card', characteristics=characteristics)
    raw = json.dumps(dict(cards=[dict(card, card_digest='sha256:' + '1' * 64)]), sort_keys=True).encode()
    path = box.admission / 'card-source-approved.json'
    path.write_bytes(raw)
    path.chmod(0o600)
    box.package['provenance']['fresh_cards_sha256'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
    template = box.package['manual_admission'][0]
    box.package['manual_admission'] = [dict(template, advert_id=advert_id) for advert_id in IDS]
    box.write_package()
    return dict(card, characteristics=list(reversed(characteristics)))


def _serve(box: Sandbox, web: CleanerWeb, runtime):
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=box.runtime, runtime=runtime, now_factory=lambda: NOW, cleaner_web=web,
        ads_block=_build_ads_block(runtime, box.runtime, FakePromotionSource(), write_enabled=False),
    )
    config = RegistryUploadHttpEntrypointConfig(
        host='127.0.0.1', port=0, upload_path='/v1/registry-upload',
        sheet_plan_path='/v1/sheet-vitrina-v1/plan', sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',
        sheet_status_path='/v1/sheet-vitrina-v1/status', sheet_operator_ui_path='/v1/sheet-vitrina-v1/operator',
        runtime_dir=box.runtime,
    )
    server = build_registry_upload_http_server(config, entrypoint=entrypoint)
    server.RequestHandlerClass.log_message = lambda *args: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = 'http://127.0.0.1:' + str(server.server_port)
    return SimpleNamespace(server=server, thread=thread, base_url=base_url,
                           url=base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + '?tab=ads&ads_tab=keyword-cleaner')


def _login(page, server):
    response = page.request.post(server.base_url + '/login', form={
        'username': 'owner', 'password': PASSWORD, 'next': server.url.split(server.base_url)[1],
    })
    assert response.status == 200
    page.goto(server.url, wait_until='domcontentloaded')
    expect(page.locator('[data-keyword-cleaner]')).to_be_visible()
    try:
        expect(page.locator('[data-kc-batch-open]')).to_be_enabled()
    except AssertionError:
        raise AssertionError('batch disabled: ' + page.locator('[data-keyword-cleaner]').inner_text()[:2500] + '\n' + page.request.get(server.base_url + '/v1/sheet-vitrina-v1/ads/keyword-cleaner/summary').text())


def _advance(parent, child, service, batch_id, owner, stop_index, clock, *, max_ticks=1500):
    """Drive saved coordinators, without browser API stubs or a background timer."""
    for _ in range(max_ticks):
        saved = service.manual_batch_snapshot(batch_id, owner)
        if saved['current_index'] >= stop_index or saved['state'] in {'complete', 'partial', 'failed'}:
            return batch_status(service, batch_id, owner)
        # The child owns its stage transitions. The parent needs a tick only
        # to dispatch the next child or to record a terminal child outcome.
        # Recomputing the full 26-row projection on every child stage is only
        # a test-driver cost, not a required worker schedule.
        pending = child.pending_jobs()
        if pending:
            child.tick()
            job = service.manual_job(pending[0], owner)
            delay = float(job.get('next_readback_at') or 0) - (clock.base.timestamp() + clock.seconds)
            if delay > 0:
                # Only the synthetic coordinator's retry clock advances.
                # Browser, HTTP authentication and fake WB remain real.
                clock.advance(delay + 0.001)
        else:
            parent.tick()
    raise AssertionError('worker did not reach selected pair: ' + repr(service.manual_batch_snapshot(batch_id, owner)))


def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    started = time.monotonic()
    with Sandbox() as box, patch.object(recovery_smoke, 'ACCOUNT', Account('seller', CHANGE_REGISTRY_ACCOUNT_SCOPE)):
        box.package['account_scope'] = CHANGE_REGISTRY_ACCOUNT_SCOPE
        box.env.write_text(box.env.read_text().replace('CHANGE_REGISTRY_ACCOUNT_SCOPE=scope',
                                                       'CHANGE_REGISTRY_ACCOUNT_SCOPE=' + CHANGE_REGISTRY_ACCOUNT_SCOPE))
        runtime = _seed_runtime(box.runtime)
        ChangeRegistryRepository(box.runtime).initialize_schema()
        card = _card_package(box)
        preview = box.execute('preview')
        box.execute('apply', expected_prestate=preview['prestate_sha256'],
                    expected_candidate=preview['candidate_sha256'])
        service = box.service()
        fake = FakeWB()
        for advert_id in IDS[1:]:
            fake.targets[advert_id] = copy.deepcopy(fake.targets[11])
        clock = Clock()
        clock.base = datetime.now(timezone.utc) + timedelta(seconds=1)
        admitted = [dict(advert_id=advert_id, nm_id=101, state='verified',
                         campaign_name='CPM ' + str(advert_id)) for advert_id in IDS]
        catalog = [Target(advert_id, 101, name='CPM ' + str(advert_id), contract_verified=True)
                   for advert_id in IDS]
        auth_env = dict(WB_CORE_WEB_AUTH_REQUIRED='1', WB_CORE_WEB_AUTH_USERNAME='owner',
                        WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(PASSWORD),
                        WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-browser-recovery-secret')
        with fake.server() as url, patch.dict(os.environ, auth_env, clear=False):
            source = CleanerWbSource(
                account=service.account, runtime=OfficialApiRuntimeConfig('synthetic', url, 2), fixture=True,
                clock=clock, monotonic=clock.monotonic,
                limiter=AccountLimiter(monotonic=clock.monotonic, sleep=clock.advance),
            )
            original_cleaner = stage_e.KeywordCleaner
            with patch.object(self_service, 'time', SimpleNamespace(time=lambda: clock.base.timestamp() + clock.seconds)), \
                 patch.object(CleanerWbSource, 'from_env', return_value=source), \
                 patch.object(stage_e, 'fetch_current_card', side_effect=lambda nm_id: dict(card)), \
                 patch.object(stage_e, 'KeywordCleaner', side_effect=lambda *args, **kwargs: original_cleaner(*args, clock=clock, **kwargs)):
                web = CleanerWeb(service, generation='monolith', approved_targets=admitted,
                                 batch_catalog_targets=catalog)
                web.worker_status = lambda: 'ready'
                web.worker_alive = lambda: True
                server = _serve(box, web, runtime)
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch()
                        page = browser.new_page(viewport={'width': 1280, 'height': 900})
                        _login(page, server)
                        page.locator('[data-kc-batch-open]').click()
                        expect(page.locator('[data-kc-batch-count]')).to_contain_text('Выбрано пар: 26')
                        page.locator('[data-kc-batch-start]').click()
                        expect(page.locator('[data-kc-message]')).to_contain_text('Массовая чистка принята')
                        with service.store.read() as connection:
                            requests = connection.execute("SELECT request_id,outcome FROM cleaner_requests WHERE account=? AND route='manual-batches'", (service.key,)).fetchall()
                        assert len(requests) == 1
                        batch_id = json.loads(requests[0]['outcome'])['batch_id']
                        owner = Principal('owner', True, True, True, site_owner=True)
                        adapter = LocalStageEAdapter(runtime_dir=box.runtime, env_file=box.env,
                                                     admission_dir=box.admission)
                        parent = BatchCleanerCoordinator(service, generation='monolith', source_factory=lambda: source,
                                                         fixture_admission=admitted, bootstrap_owner_username='owner')
                        child = ManualCleanerCoordinator(service, adapter, bootstrap_owner_username='owner')
                        before = _advance(parent, child, service, batch_id, owner, 10, clock)
                        assert before['current_index'] == 10 and before['done_count'] == 10
                        assert len(fake.writes) == 10
                        checks.append('first_ten_pairs_use_real_coordinators_and_fake_wb')

                        # A saved write readback question stops the parent on
                        # page 2. The backend smoke separately proves the
                        # physical SQLite reader-lock/COMMIT rollback.
                        parent.tick()
                        job_id = batch_child_id(batch_id, 10)
                        for _ in range(30):
                            job = service.manual_job(job_id, owner)
                            if job['stage'] == 'write_ready':
                                break
                            child.tick()
                        else:
                            raise AssertionError('eleventh child did not reach write preview')
                        hit = []
                        original_renew = KeywordCleaner.renew_lease
                        original_launch = child._launch
                        def rolled_back_renew(self, *args, **kwargs):
                            if kwargs.get('phase') == 'waiting_rate_limit' and not hit:
                                hit.append(True)
                                raise CleanerTransactionRolledBack('commit_rolled_back')
                            return original_renew(self, *args, **kwargs)
                        def defer_readback(action, *args, **kwargs):
                            if action == 'readback':
                                return {'state': 'ambiguous'}
                            return original_launch(action, *args, **kwargs)
                        with patch.object(KeywordCleaner, 'renew_lease', rolled_back_renew), \
                             patch.object(child, '_launch', side_effect=defer_readback):
                            child.tick()
                        assert hit and len(fake.writes) == 10
                        with service.store.read() as connection:
                            op = connection.execute('SELECT operation_id,state,dispatch_count FROM cleaner_write_operations WHERE run_id=?',
                                                    (job['write_run_id'],)).fetchone()
                            binding = connection.execute("SELECT 1 FROM cleaner_events WHERE run_id=? AND kind='stage_e_manual_binding'",
                                                         (job['write_run_id'],)).fetchone()
                            readback_job = connection.execute('SELECT 1 FROM cleaner_readback_jobs WHERE operation_id=?',
                                                              (op['operation_id'],)).fetchone()
                        assert op['state'] == 'prepared' and op['dispatch_count'] == 0 and binding and not readback_job
                        guard = AdmissionGuard(box.admission, service.store)
                        with guard._lock():
                            assert op['operation_id'] not in guard._load()['seals']
                        clock.advance(200)
                        service.record_manual_job(job_id, state='partial', stage='write_apply_claimed',
                                                  can_recheck=True, error_code='readback_unresolved',
                                                  error='Сохранённую операцию нужно уточнить')
                        paused = parent.tick()
                        assert paused['state'] == 'attention_required' and paused['current_index'] == 10
                        assert paused['items'][10]['job_id'] == job_id and paused['items'][10]['confirmed_excluded'] == 0, paused['items'][10]
                        assert paused['items'][10]['delivery_state'] == 'local_prepared' and paused['items'][10]['pending_count'] == 0
                        assert paused['items'][10]['not_sent_count'] and paused['items'][10]['not_sent_count'] > 0
                        assert len(fake.writes) == 10
                        page.reload(wait_until='domcontentloaded')
                        expect(page.locator('[data-kc-batch-result]')).to_contain_text('Требуется уточнение')
                        expect(page.locator('[data-kc-batch-result] > .kc-manual-card > button')).to_be_visible()
                        expect(page.locator('[data-kc-batch-result] > .kc-manual-card > button')).to_be_enabled()
                        expect(page.locator('[data-kc-batch-result]')).to_contain_text('Пары 1–10 из 26')
                        page.screenshot(path=str(output / 'attention-page-one.png'), full_page=True)
                        with page.expect_response(lambda response: response.url.endswith('/manual-clean/' + job_id + '/recheck')) as recheck_response:
                            page.locator('[data-kc-batch-result] > .kc-manual-card > button').click()
                        assert recheck_response.value.status == 202, recheck_response.value.text()
                        with service.store.read() as connection:
                            rechecks = connection.execute("SELECT request_id FROM cleaner_requests WHERE account=? AND route=?", (service.key, 'manual-clean/' + job_id + '/recheck')).fetchall()
                        assert len(rechecks) == 1
                        assert len(fake.writes) == 10
                        checks.append('reload_upper_action_rechecks_same_child_without_second_batch_or_wb_send')

                        child = ManualCleanerCoordinator(service, adapter, bootstrap_owner_username='owner')
                        parent = BatchCleanerCoordinator(service, generation='monolith', source_factory=lambda: source,
                                                         fixture_admission=admitted, bootstrap_owner_username='owner')
                        final = _advance(parent, child, service, batch_id, owner, len(IDS), clock)
                        if final['state'] not in {'complete', 'partial', 'failed'}:
                            final = parent.tick()
                        assert final['state'] == 'complete', final
                        assert final['done_count'] == 26 and final['confirmed_count'] == 26
                        assert len(fake.writes) == 26 and len({write['advert_id'] for write in fake.writes}) == 26
                        page.reload(wait_until='domcontentloaded')
                        expect(page.locator('[data-kc-batch-result]')).to_contain_text('Массовая чистка · Выполнено')
                        expect(page.locator('[data-kc-batch-result]')).to_contain_text('обработано: 26')
                        checks.append('same_batch_completes_with_truthful_26_pair_counts')
                        page.unroute_all(behavior='wait')
                        page.close()
                        browser.close()
                finally:
                    server.server.shutdown()
                    server.server.server_close()
                    server.thread.join(timeout=5)
    receipt = dict(passed=len(checks), checks=checks, fake_wb_writes=len(fake.writes), production_wb_writes=0,
                   elapsed_seconds=round(time.monotonic() - started, 1))
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/tmp/wbc-cleaner-write-recovery-browser')
    print(json.dumps(run(target), ensure_ascii=False, indent=2))
