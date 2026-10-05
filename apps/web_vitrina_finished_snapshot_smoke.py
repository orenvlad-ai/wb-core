#!/usr/bin/env python3
"""Focused background-builder/admission/bootstrap/current-period checks."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_snapshot_pilot_smoke import _build, PERIODS
from apps.web_vitrina_finished_snapshot_build import deadline_seconds, systemd_admission, bounded_worker, storage_admission
from packages.adapters.registry_upload_http_entrypoint import (
    build_registry_upload_http_server, RegistryUploadHttpEntrypointConfig,
)
from packages.application.registry_upload_http_entrypoint import SheetVitrinaV1OperatorJobStore
from packages.application.web_vitrina_finished_snapshot_builder import build_current_periods
from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers, api_jobs_admission
from packages.application.web_vitrina_snapshot_pilot import (
    SnapshotPilotError, read_current_period, publish_current_periods, retain_current_periods,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expect_error(function, reason):
    try:
        function()
    except (SnapshotPilotError, ValueError) as exc:
        assert reason in str(exc), str(exc)
    else:
        raise AssertionError(reason)


def wait_job(store, job):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = store.get(job['job_id'])
        if result['status'] in {'success', 'error'}:
            return result
        time.sleep(.005)
    raise AssertionError('job did not finish')


def check_admission():
    with tempfile.TemporaryDirectory() as temporary, patch(
        'packages.application.web_vitrina_snapshot_admission.process_identity',
        side_effect=lambda pid: 'test-current' if pid == os.getpid() else None,
    ):
        runtime = Path(temporary)
        assert api_jobs_admission(runtime) == 'unknown'
        store = SheetVitrinaV1OperatorJobStore(lambda: 'now')
        store.enable_snapshot_admission(runtime)
        assert api_jobs_admission(runtime) == 'idle'
        finish = threading.Event()
        first = store.start(operation='refresh', runner=lambda log: (finish.wait(2), {})[1])
        second = store.start(operation='refresh_group', runner=lambda log: (finish.wait(2), {})[1])
        assert api_jobs_admission(runtime) == 'busy'
        expect_error(lambda: store.enable_snapshot_admission(runtime), 'active')
        finish.set()
        assert wait_job(store, first)['status'] == 'success'
        assert wait_job(store, second)['status'] == 'success'
        assert api_jobs_admission(runtime) == 'idle'
        def failure(log):
            raise RuntimeError('expected')
        failed = store.start(operation='auto_update', runner=failure)
        assert wait_job(store, failed)['status'] == 'error'
        assert api_jobs_admission(runtime) == 'idle'
        with patch('threading.Thread.start', side_effect=RuntimeError('start failed')):
            try:
                store.start(operation='refresh', runner=lambda log: {})
            except RuntimeError:
                pass
        assert api_jobs_admission(runtime) == 'idle'
        root = store._snapshot_markers.root
        (root / 'job-dead.json').write_text(json.dumps({'pid': 999999, 'identity': 'old', 'job_id': 'dead', 'operation': 'refresh'}))
        assert api_jobs_admission(runtime) == 'idle'
        (root / 'job-invalid.json').write_text('{}')
        assert api_jobs_admission(runtime) == 'unknown'
        (root / 'job-invalid.json').unlink()
        with patch('packages.application.web_vitrina_snapshot_admission._atomic', side_effect=OSError('write failed')):
            job = store.start(operation='refresh', runner=lambda log: {})
        assert wait_job(store, job)['status'] == 'success'
        assert api_jobs_admission(runtime) == 'unknown'
    print('API admission: shared concurrent jobs, exception/finally, start failure, dead/invalid markers, fail-open business/fail-closed builder PASS')


def check_deadline():
    assert deadline_seconds(datetime(2026, 10, 3, 21, 55, 15, tzinfo=timezone.utc)) == 225
    assert deadline_seconds(datetime(2026, 10, 3, 21, 58, 55, tzinfo=timezone.utc)) == 5
    assert deadline_seconds(datetime(2026, 10, 3, 21, 59, tzinfo=timezone.utc)) == 0
    assert deadline_seconds(datetime(2026, 10, 3, 22, 55, tzinfo=timezone.utc)) == 0
    inactive = SimpleNamespace(stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\n')
    assert systemd_admission(lambda *a, **k: inactive) == 'idle'
    active = SimpleNamespace(stdout='LoadState=loaded\nActiveState=activating\nMainPID=12\n')
    assert systemd_admission(lambda *a, **k: active) == 'busy'
    assert systemd_admission(lambda *a, **k: SimpleNamespace(stdout='LoadState=not-found\n')) == 'unknown'
    assert bounded_worker([sys.executable, '-c', 'import time; time.sleep(1)'], .05)['status'] == 'skipped_deadline'
    with tempfile.TemporaryDirectory() as temporary:
        runtime = Path(temporary)
        expect_error(lambda: storage_admission(runtime, runtime / 'generations/web-vitrina-finished.sqlite3'), 'contract')
        assert not (runtime / 'generations').exists()
    print('absolute deadline/process kill, oneshot activating/unknown and missing mount before writes PASS')


def check_builder_and_store(server):
    store = server.snapshot_pilot_store
    now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    # Legacy trusted result and builder use the same table evaluator.
    server.entrypoint.now_factory = lambda: now
    server.entrypoint.web_vitrina_block.now_factory = lambda: now
    compositions = {(start, end): _build(server, start, end, compact=True) for start, end in PERIODS}
    sources = [server.entrypoint.runtime.db_path]
    book = server.runtime_dir / 'fbs-snapshot-accounting.sqlite3'
    if book.exists():
        sources.append(book)
    before = [digest(path) for path in sources]
    rename = os.replace
    def checked_rename(source, destination):
        assert not store.with_name(store.name + '.initialized').exists()
        return rename(source, destination)
    with patch('packages.application.web_vitrina_finished_snapshot_builder.os.replace', side_effect=checked_rename):
        result = build_current_periods(server.runtime_dir, store, now=now)
    assert result['status'] == 'published' and store.exists()
    assert store.with_name(store.name + '.initialized').exists()
    assert [digest(path) for path in sources] == before
    originals = {}
    for days, (start, end) in zip((14, 31), PERIODS):
        summary = read_current_period(store, period_days=days, part='summary', business_today=end)
        generation = summary['snapshot_pilot']['generation_id']
        sku = read_current_period(store, period_days=days, part='sku', business_today=end, generation_id=generation)
        combined = summary['table_surface']['rows'] + sku['rows']
        by_id = {row['row_id']: row for row in combined}
        expected = compositions[(start, end)]['table_surface']
        assert [by_id[row] for row in sku['catalog_order']] == expected['rows']
        assert summary['table_surface']['columns'] == expected['columns']
        assert sku['groupings'] == expected['groupings']
        originals[days] = generation
    expect_error(lambda: read_current_period(store, period_days=14, part='sku', business_today='2026-04-20',
                  generation_id=originals[31]), 'generation_period_mismatch')
    unchanged = read_current_period(store, period_days=14, part='summary', business_today='2026-04-20')['snapshot_pilot']['generation_id']
    invalid = deepcopy(compositions)
    invalid[PERIODS[1]]['meta']['current_state'] = 'error'
    expect_error(lambda: publish_current_periods(store, invalid), 'finished_ready')
    assert read_current_period(store, period_days=14, part='summary', business_today='2026-04-20')['snapshot_pilot']['generation_id'] == unchanged
    stale = read_current_period(store, period_days=14, part='summary', business_today='2026-04-21')
    assert stale['snapshot_pilot']['stale_period'] and stale['snapshot_pilot']['date_to'] == '2026-04-20'
    expect_error(lambda: build_current_periods(server.runtime_dir, server.entrypoint.runtime.db_path, now=now), 'separate')
    linked = server.runtime_dir / 'hardlink.sqlite3'
    os.link(server.entrypoint.runtime.db_path, linked)
    expect_error(lambda: build_current_periods(server.runtime_dir, linked, now=now), 'separate')
    linked.unlink()
    for index in range(4):
        value = deepcopy(compositions)
        for composition in value.values():
            composition['test_generation'] = index
        publish_current_periods(store, value)
    removed = retain_current_periods(store, now=datetime.now(timezone.utc) + timedelta(hours=5))
    assert removed >= 4
    expect_error(lambda: read_current_period(store, period_days=14, part='sku', business_today='2026-04-20',
                  generation_id=originals[14]), 'snapshot_expired')
    print('read-only existing evaluator parity14/31, atomic failed pair, bootstrap, stale date, collision/hardlink, retention/expired PASS')


def check_killed_publish(server):
    store = server.snapshot_pilot_store
    previous = read_current_period(store, period_days=14, part='summary', business_today='2026-04-20')['snapshot_pilot']['generation_id']
    marker = server.runtime_dir / 'uncommitted-writer-ready'
    candidate = store.with_name(store.name + '.building-crash-test')
    __import__('shutil').copyfile(store, candidate)
    files_before = {path.name for path in store.parent.iterdir()}
    code = """import sqlite3,sys,time
+from pathlib import Path
+c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA cache_size=8');c.execute('BEGIN IMMEDIATE')
+for i in range(16):
+ c.execute('INSERT INTO web_vitrina_pilot_generations SELECT ?,date_from,date_to,assembled_at,?,sku_json,summary_digest,sku_digest,summary_rows,sku_rows FROM web_vitrina_pilot_generations LIMIT 1', ('uncommitted_'+str(i), b'x'*1048576))
+Path(sys.argv[2]).write_text('ready');time.sleep(10)
+""".replace('\n+', '\n')
    process = subprocess.Popen([sys.executable, '-c', code, str(candidate), str(marker)])
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists(), 'writer did not reach uncommitted state'
        process.kill()
        process.wait(timeout=2)
        # First operation after SIGKILL is a strictly read-only request, not rw
        # recovery or another builder. The unpublished blob stays invisible.
        selected = read_current_period(store, period_days=14, part='summary', business_today='2026-04-20')
        assert selected['snapshot_pilot']['generation_id'] == previous
        after_read = {path.name for path in store.parent.iterdir()}
        assert not any(name.startswith(store.name + '-wal') or name.startswith(store.name + '-shm') for name in after_read - files_before)
        assert candidate.exists() and Path(str(candidate) + '-journal').exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
    candidate.unlink(missing_ok=True)
    Path(str(candidate) + '-journal').unlink(missing_ok=True)
    print('SIGKILL uncommitted candidate -> read-only last-good, no recovery write or GET sidecars PASS')


def check_http_ui(server):
    from playwright.sync_api import sync_playwright
    server.server.shutdown()
    server.server.server_close()
    server.thread.join(timeout=3)
    config = RegistryUploadHttpEntrypointConfig(host='127.0.0.1', port=0, upload_path='/v1/registry-upload/bundle',
        sheet_plan_path='/v1/sheet-vitrina-v1/plan', sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',
        sheet_status_path='/v1/sheet-vitrina-v1/status', sheet_operator_ui_path='/sheet-vitrina-v1/operator', runtime_dir=server.runtime_dir)
    store = server.snapshot_pilot_store
    server.server = build_registry_upload_http_server(config, entrypoint=server.entrypoint,
        snapshot_pilot_store=store, finished_snapshots_default=True)
    server.thread = threading.Thread(target=server.server.serve_forever, daemon=True)
    server.thread.start()
    base = f'http://127.0.0.1:{server.server.server_address[1]}'
    calls = []
    config_reads = []
    config_saves = []
    from apps.sheet_vitrina_v1_web_vitrina_user_config_browser_smoke import (
        WEIGHTED_SELLER_PRICE_LOGICAL_ID, SELLER_PRICE_DISCOUNTED_METRIC_KEY,
    )
    config_payload = {'version': 5, 'presentation': {
        'order': [WEIGHTED_SELLER_PRICE_LOGICAL_ID, 'total::total_orderSum'],
        'display': {WEIGHTED_SELLER_PRICE_LOGICAL_ID: 'collapsed', 'total::total_orderSum': 'hidden'},
        'manual': True}, 'expanded_anchors': [],
        'sku_presets': [{'preset_id': 'focused', 'name': 'Сохранён', 'metric_keys': [SELLER_PRICE_DISCOUNTED_METRIC_KEY]}],
        'sku_metric_selection': {'mode': 'preset', 'preset_id': 'focused', 'all': False, 'metric_keys': []},
        'migrations': {'incident_effective_shown_v1': True, 'sku_presets_seeded_v1': True,
                       'unified_presentation_v1': True, 'seller_price_weighted_v1': True}}
    def config_read(**kwargs):
        config_reads.append(kwargs)
        return {'status': 'ok', 'revision': 1, 'config': config_payload, 'schema_version': 2}
    def config_save(**kwargs):
        config_saves.append(kwargs)
        return {'status': 'ok', 'revision': 2, 'config': kwargs['payload']['config'], 'schema_version': 2}
    server.entrypoint.handle_sheet_web_vitrina_user_config_request = config_read
    server.entrypoint.handle_sheet_web_vitrina_user_config_save_request = config_save
    legacy = server.entrypoint.handle_sheet_web_vitrina_page_composition_request
    server.entrypoint.handle_sheet_web_vitrina_page_composition_request = lambda **kwargs: (calls.append(kwargs), legacy(**kwargs))[1]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        requests = []
        page.on('request', lambda request: requests.append(request.url) if '/v1/sheet-vitrina-v1/web-vitrina?' in request.url else None)
        page.goto(base + '/sheet-vitrina-v1/vitrina')
        page.wait_for_function("!document.querySelector('[data-snapshot-pilot-toolbar]').hidden")
        assert not calls, calls
        assert len(config_reads) == 1
        page.locator('[data-metrics-settings-open]').click()
        weighted = page.locator('[data-metric-config-row="' + WEIGHTED_SELLER_PRICE_LOGICAL_ID + '"]')
        assert weighted.get_attribute('data-metric-display-status') == 'collapsed'
        assert page.locator('[data-metric-config-row="total::total_orderSum"]').get_attribute('data-metric-display-status') == 'hidden'
        weighted.locator('[data-metric-display-select]').select_option('shown')
        page.wait_for_timeout(400)
        assert config_saves, 'ready mode must retain server-side preference saving'
        page.keyboard.press('Escape')
        assert len(requests) == 1 and 'period_days=14' in requests[0], requests
        # Existing history capabilities survive persisted ready responses.
        assert page.locator('[data-history-preset="rolling_30"]').count() == 1
        assert page.locator('[data-history-preset="finished_31"]').count() == 1
        page.locator('[data-history-toggle]').click()
        page.locator('[data-history-preset="finished_31"]').click()
        page.wait_for_function("new URLSearchParams(location.search).get('date_from') !== null")
        page.wait_for_timeout(150)
        assert any('period_days=31' in url and 'part=summary' in url for url in requests), requests
        assert not calls
        page.locator('[data-filters-toggle]').click()
        page.locator('[data-snapshot-pilot-expand]').click()
        page.locator('[data-snapshot-pilot-toolbar]').wait_for(state='hidden')
        page.locator('[data-filters-close]').click()
        assert any('part=sku' in url and 'generation_id=' in url for url in requests)
        assert page.locator('[data-load-status-text]').get_attribute('data-load-status-text') == 'предыдущий готовый снимок'
        # Same-page transition through the existing 30-day preset.
        page.locator('[data-history-toggle]').click()
        page.locator('[data-history-preset="rolling_30"]').click()
        page.locator('[data-history-save]').click()
        page.locator('[data-load-refresh-button]').wait_for(state='visible')
        page.wait_for_timeout(150)
        assert calls
        assert page.locator('[data-snapshot-pilot-toolbar]').is_hidden()
        calls.clear()
        page.locator('[data-history-toggle]').click()
        page.locator('[data-history-preset="finished_31"]').click()
        page.wait_for_function("!document.querySelector('[data-snapshot-pilot-toolbar]').hidden")
        assert not calls
        # Existing corrupt or missing+initialized store stays in fast mode and never evaluates.
        calls.clear()
        preserved = store.read_bytes()
        store.write_bytes(b'corrupt')
        page.goto(base + '/sheet-vitrina-v1/vitrina')
        page.wait_for_timeout(150)
        assert not calls
        assert page.locator('[data-snapshot-pilot-toolbar]').is_hidden()
        store.write_bytes(preserved)
        store.unlink()
        page.goto(base + '/sheet-vitrina-v1/vitrina')
        page.wait_for_timeout(150)
        assert not calls
        store.write_bytes(preserved)
        browser.close()
    print('ordinary default14, visible fast31 preset, pinned SKU/stale warning, unsupported legacy, corrupt/missing no fallback PASS')


def main():
    check_admission()
    check_deadline()
    server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=31, snapshot_pilot=True)
    with server:
        check_builder_and_store(server)
        check_killed_publish(server)
        check_http_ui(server)
    print('finished snapshot focused smoke PASS')


if __name__ == '__main__':
    main()
