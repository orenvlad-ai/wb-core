"""Actual common HTTP journal: native supplier grants, aliases and RO reads."""
from contextlib import ExitStack, closing
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_supplier_shipments_http_smoke import server_for, request, stop
from apps.operator_supplier_factual_dates_smoke import fixture, SHIPMENT_ID
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application import operator_supplier_shipments as source
from packages.application import operator_supplier_factual_dates as factual

PATH = '/v1/sheet-vitrina-v1/operations'


def scope(name, role='operator'):
    return 'webcore_user_' + hashlib.sha256((role + ':' + name).encode()).hexdigest()[:32]


def auth(name, role='operator', sections=('supply',)):
    stack = ExitStack()
    stack.enter_context(patch.object(http, '_web_auth_config', return_value={'configured': True, 'enabled': True}))
    stack.enter_context(patch.object(http, '_authenticated_web_user', return_value={
        'username': name, 'role': role, 'allowed_sections': list(sections)}))
    return stack


def main():
    with TemporaryDirectory(prefix='supplier-journal-http-') as raw:
        rt, entry, payload = seed(raw)
        saved = entry.handle_supplier_shipments_create_request(payload, actor='alice', request_scope=scope('alice'))
        identity = saved['acceptance']['operation_id']
        safe = entry.handle_supplier_shipments_create_request({**payload, 'request_id': 'supplier-safe-request'},
            actor='vendor', request_scope=scope('vendor', 'supplier'))
        safe_id = safe['acceptance']['operation_id']
        server, thread, base = server_for(entry)
        before = rt.db_path.read_bytes()
        try:
            with auth('alice'):
                status, listing = request(base, PATH + '?domain=supplier_shipment&limit=1')
                assert status == 200 and listing['total'] == 1 and not listing['has_more'], listing
                assert listing['items'][0]['operation_id'] == identity
                assert request(base, PATH + '?domain=supplier_shipment&page=2&limit=1')[1]['items'] == []
                assert request(base, PATH + '?domain=supplier_shipment&search=missing-private-text')[1]['total'] == 0
                assert request(base, PATH + '/' + identity)[1]['operation']['operation_id'] == identity
                assert request(base, PATH + '/' + safe_id)[0] == 404
            with auth('foreign'):
                assert request(base, PATH + '?domain=supplier_shipment')[1]['total'] == 0
                assert request(base, PATH + '/' + identity)[0] == 404
            with auth('reports-only', sections=('reports',)):
                listing = request(base, PATH + '?domain=supplier_shipment')[1]
                assert listing['total'] == 0 and not listing['items']
                assert request(base, PATH + '/' + identity)[0] == 404
            with auth('vendor', role='supplier', sections=()):
                listing = request(base, PATH)[1]
                assert listing['total'] == 1 and listing['items'][0]['operation_id'] == safe_id, listing
                assert {x['domain'] for x in listing['available_domains']} == {source.DOMAIN}
                assert request(base, PATH + '?search=internal_nm_id')[1]['total'] == 0
                assert request(base, PATH + '?search=2026-10-10')[1]['total'] == 1
                safe_receipt = request(base, PATH + '/' + safe_id)[1]['operation']
                assert 'affected_nm_ids' not in safe_receipt['processing']
                assert request(base, PATH + '/' + identity)[0] == 404
                assert request(base, PATH + '?domain=supplier_factual_date')[1]['total'] == 0
            assert before == rt.db_path.read_bytes(), 'GET wrote source state'
        finally:
            stop(server, thread)
    with TemporaryDirectory(prefix='factual-journal-http-') as raw:
        rt, entry = fixture(raw)
        preview = entry.handle_supplier_factual_dates_preview_request(SHIPMENT_ID, {'actual_shipment_date': '2026-06-26'})
        body = {'request_id': 'factual-journal-alias1', 'confirmation_token': preview['confirmation_token']}
        with patch.object(rt, 'complete_supplier_confirmation_preview', side_effect=RuntimeError('lost auxiliary response')):
            one = entry.handle_supplier_factual_dates_confirm_request(SHIPMENT_ID, body, actor='alice', request_scope=scope('alice'))
            two = entry.handle_supplier_factual_dates_confirm_request(SHIPMENT_ID, {**body, 'request_id': 'factual-journal-alias2'}, actor='alice', request_scope=scope('alice'))
        identity = one['acceptance']['operation_id']
        assert identity == two['acceptance']['operation_id']
        with closing(source.readonly(rt.db_path)) as conn:
            assert conn.execute(f'SELECT count(*) FROM {factual.TABLE}').fetchone()[0] == 2
        server, thread, base = server_for(entry)
        before = rt.db_path.read_bytes()
        try:
            with auth('alice'):
                listing = request(base, PATH + '?domain=supplier_factual_date&limit=1')[1]
                assert listing['total'] == 1 and not listing['has_more'], listing
                assert listing['items'][0]['operation_id'] == identity
                assert request(base, PATH + '?domain=supplier_factual_date&page=2&limit=1')[1]['items'] == []
                assert request(base, PATH + '?domain=supplier_factual_date&search=missing')[1]['total'] == 0
                detail = request(base, PATH + '/' + identity)[1]['operation']
                assert detail['operation_id'] == identity and not detail['physical_applied']
                assert not detail['processing']['complete']
            with auth('foreign'):
                assert request(base, PATH + '?domain=supplier_factual_date')[1]['total'] == 0
                assert request(base, PATH + '/' + identity)[0] == 404
            with auth('alice', sections=('reports',)):
                assert request(base, PATH + '?domain=supplier_factual_date')[1]['total'] == 0
                assert request(base, PATH + '/' + identity)[0] == 404
            assert before == rt.db_path.read_bytes(), 'factual GET wrote source state'
        finally:
            stop(server, thread)
    print('Common supplier journal: scoped grants before counts/search/pages/detail; safe supplier; native factual alias dedup; query-only: OK')


if __name__ == '__main__':
    main()
