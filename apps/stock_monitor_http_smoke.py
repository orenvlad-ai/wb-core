"""Monitoring HTTP contract: private cached reads, explicit bounded refresh, shell embed."""
from pathlib import Path
import json
import sys
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
    print('stock monitor HTTP smoke: PASS')


if __name__ == '__main__': main()
