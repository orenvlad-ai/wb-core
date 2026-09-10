#!/usr/bin/env python3
"""Authenticated loopback-only Web Vitrina with synthetic cleaner/ads data.

No WB source, credentials, worker thread or timer is installed. --serve supports
manual UI review; every run creates a new disposable runtime.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request
import urllib.error
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
from apps.sheet_vitrina_v1_ads_smoke import FakePromotionSource, _seed_runtime, _build_ads_block, NOW
from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server, DEFAULT_SHEET_WEB_VITRINA_UI_PATH
from packages.adapters.search_cluster_cleaner_http import PREFIX
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account, Principal, Target, digest
from packages.domain.search_cluster_sources import union_snapshot

PASSWORD = 'cleaner-fixture-only'
OWNER = Principal('owner', True, True, True)
FIXTURE_NOW = '2026-09-11T01:59:00Z'
GENERATION = 'synthetic-stage-c'
PROFILE = dict(nm_id=101, version=1, category='phone_screen_glass', models=['16 promax'], kind='clean', frame='black', source='Синтетическая проверка совместимости', verified_at='2026-09-10T00:00:00Z')
QUERIES = ['стекло iphone 16 pro max без салфетки', 'стекло iphone 16 pro max не remax', 'стекло iphone 16 pro max без <img src=x onerror="window.cleanerXss=1">']


def seed_cleaner(runtime_dir: Path, mode='normal'):
    cleaner = KeywordCleaner(CleanerStore(StoreRegistry(runtime_dir)), Account('synthetic-seller', 'synthetic-account'), owner_username='owner', clock=lambda: FIXTURE_NOW)
    cleaner.initialize(generation=GENERATION)
    if mode == 'unready': return cleaner
    rows = [dict(advert_id=1, nm_id=101, query='стекло iphone 16 pro max', decision='allow', observed_state='active', provenance={'synthetic': True})]
    cleaner.import_baseline(rows, [PROFILE], provenance={'synthetic': True}, expected_digest=digest(rows), ready=True)
    with cleaner.store.transaction() as c:
        c.execute('UPDATE cleaner_settings SET restore_hold=0 WHERE account=?', (cleaner.key,))
    cleaner.update_settings(dict(enabled=True, expected_revision=1, request_id='fixture-settings'), OWNER)
    cleaner.heartbeat(generation=GENERATION)
    run_id = cleaner.start_run(dict(request_id='fixture-initial-scan'), OWNER)['run_id']
    run = cleaner.claim_run(generation=GENERATION)
    target = Target(10101, 101, contract_verified=True)
    unknown = Target(10102, 102, contract_verified=True)
    targets = [target] + ([unknown] if mode in {'profile-required', 'partial'} else [])
    cleaner.sync_catalog(run_id, run['worker_token'], GENERATION, targets, [])
    for t in targets:
        if mode == 'partial' and t == unknown:
            cleaner.record_target_error(run_id, run['worker_token'], GENERATION, t, 'source_temporarily_unavailable')
            continue
        queries = QUERIES if t == target and mode in {'normal', 'partial', 'profile-required'} else ['стекло iphone 17 pro'] if t == unknown else []
        snap = union_snapshot(t, list_entry=dict(active=queries, excluded=[], archived=[]), stats_queries=queries, minus_queries=[], observed_at=FIXTURE_NOW, source_times={s: FIXTURE_NOW for s in ('list', 'statistics', 'minus')})
        cleaner.record_snapshot(run_id, run['worker_token'], GENERATION, snap)
    cleaner.finish_run(run_id, run['worker_token'], GENERATION)
    if mode == 'failed':
        # Deliberately synthetic state projection, not a live worker failure claim.
        with cleaner.store.transaction() as c:
            c.execute("UPDATE cleaner_runs SET state='failed',reason='catalog_unavailable',summary='{}' WHERE run_id=?", (run_id,))
    if mode == 'unresolved':
        with cleaner.store.transaction() as c:
            c.execute("""INSERT INTO cleaner_write_operations(operation_id,account,target,run_id,state,before_json,expected_json,additions,candidate_digest,versions,dispatch_count,created_at,updated_at)
              VALUES('fixture-unresolved',?, '10101:101',?,'unresolved','[]','[]','[]','synthetic','{}',1,?,?)""", (cleaner.key, run_id, FIXTURE_NOW, FIXTURE_NOW))
    return cleaner


class Fixture:
    def request(self, path, payload=None, *, opener=None, headers=None, method=None):
        request_headers = {'Accept': 'application/json'}
        if payload is not None:
            request_headers.update({'Content-Type':'application/json', 'Origin': self.base_url, 'X-WB-Keyword-Cleaner-CSRF':'1', 'Sec-Fetch-Site':'same-origin'})
        if headers:
            for key, value in headers.items():
                if value is None: request_headers.pop(key, None)
                else: request_headers[key] = value
        request = urllib.request.Request(self.base_url + PREFIX + path, data=json.dumps(payload).encode() if payload is not None else None, headers=request_headers, method=method)
        try:
            response = (opener or self.owner).open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            return response.status, json.loads(raw), dict(response.headers)

    def login(self, username='owner'):
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(self.base_url+'/login', data=urllib.parse.urlencode({'username':username,'password':PASSWORD,'next':DEFAULT_SHEET_WEB_VITRINA_UI_PATH+('?tab=vitrina' if username=='noads' else '?tab=ads&ads_tab=keyword-cleaner')}).encode(), headers={'Content-Type':'application/x-www-form-urlencoded'})
        with opener.open(request, timeout=10) as response:
            assert response.status == 200
            response.read()
        return opener

    def count(self, table):
        assert table in {'cleaner_requests','cleaner_runs','cleaner_manual_overrides','cleaner_write_operations','cleaner_profiles'}
        with self.cleaner.store.read() as c:
            return c.execute('SELECT count(*) FROM '+table).fetchone()[0]


@contextmanager
def running_fixture(mode='normal', port=0):
    env = {key: os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG','PYTHONPATH','PLAYWRIGHT_BROWSERS_PATH') if key in os.environ}
    env.update(WB_CORE_WEB_AUTH_REQUIRED='1', WB_CORE_WEB_AUTH_USERNAME='owner', WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(PASSWORD), WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-stage-c-auth-secret')
    original_urlopen = urllib.request.urlopen
    def local_only(url, *args, **kwargs):
        address = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        if urllib.parse.urlparse(address).hostname not in {'127.0.0.1','localhost'}:
            raise AssertionError('External network is forbidden in the synthetic fixture')
        return original_urlopen(url, *args, **kwargs)
    with tempfile.TemporaryDirectory(prefix='cleaner-stage-c-') as tmp, patch.dict(os.environ, env, clear=True), patch('urllib.request.urlopen', local_only):
        fixture = Fixture();fixture.runtime_dir = Path(tmp)/'runtime'
        runtime = _seed_runtime(fixture.runtime_dir)
        fixture.cleaner = seed_cleaner(fixture.runtime_dir, mode)
        fixture.web = CleanerWeb(fixture.cleaner, generation=GENERATION)
        fixture.entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=fixture.runtime_dir, runtime=runtime, now_factory=lambda: NOW, cleaner_web=fixture.web, ads_block=_build_ads_block(runtime, fixture.runtime_dir, FakePromotionSource(), write_enabled=False))
        password_hash = _password_hash(PASSWORD)
        for username, role, sections in [('reader','operator',['ads']), ('admin','admin',['ads','sku_management']), ('noads','operator',['vitrina'])]:
            fixture.entrypoint.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-'+username,username=username,display_name=username,role=role,allowed_sections=sections,manage_users=role=='admin',password_hash=password_hash,is_active=True,created_at=FIXTURE_NOW,updated_at=FIXTURE_NOW))
        config = RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path='/v1/registry-upload',sheet_plan_path='/v1/sheet-vitrina-v1/plan',sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path='/v1/sheet-vitrina-v1/status',sheet_operator_ui_path='/v1/sheet-vitrina-v1/operator',runtime_dir=fixture.runtime_dir)
        fixture.server = build_registry_upload_http_server(config, entrypoint=fixture.entrypoint)
        fixture.server.RequestHandlerClass.log_message = lambda *args: None
        fixture.base_url = 'http://127.0.0.1:'+str(fixture.server.server_port)
        fixture.url = fixture.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + '?tab=ads&ads_tab=keyword-cleaner'
        thread = threading.Thread(target=fixture.server.serve_forever, daemon=True);thread.start()
        try:
            fixture.owner = fixture.login()
            yield fixture
        finally:
            fixture.server.shutdown();fixture.server.server_close();thread.join(timeout=5)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true',required=True);parser.add_argument('--mode',choices=['normal','empty','partial','failed','unresolved','unready','profile-required'],default='normal');parser.add_argument('--port',type=int,default=0);args=parser.parse_args()
    with running_fixture(args.mode,args.port) as fixture:
        print(json.dumps(dict(url=fixture.url,username='owner',password=PASSWORD,synthetic=True),ensure_ascii=False),flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass

if __name__=='__main__': main()
