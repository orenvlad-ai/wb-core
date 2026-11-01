"""Monitoring HTTP contract: private cached reads, explicit bounded refresh, shell embed."""
from pathlib import Path
import json
import sys
import os
import sqlite3
import threading
from types import SimpleNamespace
from urllib import request, error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_STOCK_MONITOR_PATH, _required_section_for_path, WEB_AUTH_SECTION_SUPPLY,
)


def get(url):
    try:
        with request.urlopen(url, timeout=20) as result:
            return result.status, result.headers, result.read().decode()
    except error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


def isolated_read_smoke():
    from packages.adapters import registry_upload_http_entrypoint as http
    from packages.adapters.stock_monitor_http import start_stock_monitor_contour
    from packages.application import registry_upload_db_backed_runtime as db
    from packages.application.web_vitrina_window_read_context import active_window_read_context
    from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash

    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    with fixture as base, patch.dict(os.environ, {
        'WB_CORE_WEB_AUTH_REQUIRED': '1', 'WB_CORE_WEB_AUTH_USERNAME': 'fixture-owner',
        'WB_CORE_WEB_AUTH_PASSWORD_HASH': _password_hash('fixture-password'),
        'WB_CORE_WEB_AUTH_SESSION_SECRET': 'monitor-test-only-secret',
    }):
        runtime = fixture.entrypoint.runtime
        runtime.save_sheet_vitrina_user({
            'user_id': 'monitor-reader', 'username': 'monitor-reader', 'role': 'operator',
            'display_name': 'Monitor reader', 'allowed_sections': ['supply'],
            'manage_users': False, 'password_hash': _password_hash('fixture-password'),
            'is_active': True, 'created_at': '2026-11-01T00:00:00Z',
            'updated_at': '2026-11-01T00:00:00Z',
        })
        cookie = http._build_session_cookie(SimpleNamespace(headers={'Host': 'localhost'}),
            'monitor-reader', http._web_auth_config(), role='operator', display_name='Reader').split(';')[0]
        entered, release = threading.Event(), threading.Event()
        original = fixture.server.RequestHandlerClass
        class SlowPrimary(original):
            def do_GET(self):
                if self.path == '/test-held-request':
                    entered.set()
                    assert release.wait(15), 'test did not release primary request'
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
                    return
                super().do_GET()
        fixture.server.RequestHandlerClass = SlowPrimary
        sidecar, thread = start_stock_monitor_contour(fixture.server, port=0)
        sidebase = f'http://127.0.0.1:{sidecar.server_port}'
        slow = threading.Thread(target=lambda: get(base + '/test-held-request'), daemon=True)
        def read(path=DEFAULT_STOCK_MONITOR_PATH, method='GET', auth=cookie):
            req = request.Request(sidebase + path, method=method, headers={'Cookie': auth})
            try:
                with request.urlopen(req, timeout=2) as response:
                    return response.status, response.headers, response.read()
            except error.HTTPError as exc:
                return exc.code, exc.headers, exc.read()
        original_schema = db._ensure_schema
        def query_only_schema(conn):
            assert active_window_read_context() is not None
            assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
            return original_schema(conn)
        try:
            slow.start(); assert entered.wait(2)
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute('BEGIN IMMEDIATE')
                with patch.object(db, '_ensure_schema', side_effect=query_only_schema), \
                     patch.object(fixture.entrypoint.stock_monitor, '_build_base', side_effect=AssertionError('read rebuilt snapshot')), \
                     patch.object(fixture.entrypoint, 'handle_stock_monitor_refresh_request', side_effect=AssertionError('sidecar wrote')):
                    code, headers, raw = read()
                    assert code == 200, (code, raw)
                    assert headers['Cache-Control'] == 'private, no-store'
                    assert json.loads(raw)['cache']['hit'] is False
                    assert not release.is_set() and slow.is_alive(), 'primary must still be held'
                    assert not fixture.entrypoint.stock_monitor.cache_dir.exists()
                    assert read(auth='')[0] == 401
                    assert read(auth=cookie + 'bad')[0] == 401
                    assert read(path='/test-held-request')[0] == 404
                    for method in ('POST', 'PATCH', 'DELETE', 'PUT', 'HEAD', 'OPTIONS'):
                        assert read(method=method)[0] == 405
                    assert read(path=DEFAULT_STOCK_MONITOR_PATH + '/refresh', method='POST')[0] == 405
                writer.rollback()
            # Current ACL and active flag are re-read, never cached by the sidecar.
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute("UPDATE sheet_vitrina_v1_users SET allowed_sections_json='[]' WHERE username='monitor-reader'")
            assert read()[0] == 403
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute("UPDATE sheet_vitrina_v1_users SET is_active=0 WHERE username='monitor-reader'")
            assert read()[0] == 401
            routes = json.loads((Path(__file__).resolve().parents[1] / 'artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json').read_text())['routes']
            route = next(r for r in routes if r['path'] == DEFAULT_STOCK_MONITOR_PATH)
            assert route['match'] == 'exact' and route['methods'] == ['GET']
            assert route['proxy_pass_url'] == 'http://127.0.0.1:8780'
            refresh = next(r for r in routes if r['path'] == DEFAULT_STOCK_MONITOR_PATH + '/refresh')
            assert 'proxy_pass_url' not in refresh
        finally:
            release.set(); slow.join(3)
            sidecar.shutdown(); sidecar.server_close(); thread.join(3)
        assert not thread.is_alive()


def main():
    for path in (DEFAULT_STOCK_MONITOR_PATH, DEFAULT_STOCK_MONITOR_PATH + '/refresh'):
        assert _required_section_for_path(path) == WEB_AUTH_SECTION_SUPPLY
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    with fixture as base:
        cache_dir = fixture.entrypoint.stock_monitor.cache_dir
        with patch.object(fixture.entrypoint.stock_monitor, '_build_base', side_effect=AssertionError('GET recalculated history')):
            code, headers, raw = get(base + DEFAULT_STOCK_MONITOR_PATH)
            payload = json.loads(raw)
            assert code == 200 and payload['cache']['hit'] is False, (code, raw)
            assert payload['rows'] == [] and payload['status'] == 'unavailable'
            assert headers['Cache-Control'] == 'private, no-store'
            assert not cache_dir.exists(), 'GET must not create settings/cache/job'
            assert get(base + DEFAULT_STOCK_MONITOR_PATH + '?period_days=0')[0] == 422
            assert get(base + DEFAULT_STOCK_MONITOR_PATH + '?horizon_days=999999')[0] == 422
            assert get(base + DEFAULT_STOCK_MONITOR_PATH + '?unknown=1')[0] == 422
        code, _, html = get(base + '/sheet-vitrina-v1/vitrina?tab=warehouses')
        assert code == 200
        assert '{{STOCK_MONITOR_PANEL}}' not in html
        assert 'data-stock-monitor' in html and 'data-open-stock-monitor' in html
        with patch.object(fixture.entrypoint, 'handle_stock_monitor_refresh_request', return_value={'status':'refreshing','period_days':30,'job_id':'fixture'}) as refresh:
            forbidden = request.Request(base + DEFAULT_STOCK_MONITOR_PATH + '/refresh',
                data=json.dumps({'period_days':30}).encode(), method='POST',
                headers={'Content-Type':'application/json', 'Origin':base})
            assert get(forbidden)[0] == 403
            refresh.assert_not_called()
            req = request.Request(base + DEFAULT_STOCK_MONITOR_PATH + '/refresh',
                data=json.dumps({'period_days':30}).encode(), method='POST',
                headers={'Content-Type':'application/json', 'Origin':base, 'X-WB-FF-Pool-CSRF':'1'})
            code, _, raw = get(req)
            assert code == 202, (code, raw)
            assert json.loads(raw)['period_days'] == 30
            refresh.assert_called_once_with({'period_days':30})
    isolated_read_smoke()
    print('stock monitor HTTP smoke: PASS')


if __name__ == '__main__': main()
