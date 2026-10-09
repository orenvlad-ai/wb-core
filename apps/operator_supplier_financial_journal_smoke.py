"""Actual saved financial parents/children in the scoped, query-only common journal."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup, document
from apps.operator_cny_journal_smoke import source_snapshot, get_only_connect
from apps.operator_supplier_shipments_http_smoke import server_for, stop, request, PATH
from packages.application import operator_supplier_financial as financial, operator_operations as journal
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.adapters import registry_upload_http_entrypoint as http


def main():
    with TemporaryDirectory(prefix='financial-common-journal-') as directory:
        runtime, _ = setup(directory)
        entry = RegistryUploadHttpEntrypoint(runtime=runtime, runtime_dir=runtime.runtime_dir)
        user = {'username': 'alice', 'role': 'operator', 'allowed_sections': ['supply']}
        accepted = []
        with patch.object(http, '_web_auth_config', return_value={'enabled': True, 'configured': True}), \
                patch.object(http, '_authenticated_web_user') as authenticated:
            def save(name, actor, mode):
                authenticated.return_value = {**user, 'username': actor}
                scope = http._current_web_user_config_key(None)
                manifest = [{'child_key': name + '-one', 'kind': 'financial', 'subject_id': name + '-one'},
                    {'child_key': name + '-two', 'kind': 'financial', 'subject_id': name + '-two'}]
                def write(child):
                    if mode == 'preview': return {'preview_required': True, 'active_saved': False}
                    if mode == 'reject': raise ValueError('synthetic rejected input')
                    if mode == 'partial' and child['child_key'].endswith('two'): raise RuntimeError('lost before second save')
                    return document(runtime, child)
                return financial.execute(runtime, action='confirm_upload', payload={'request_id': name},
                    shipment_id='source', actor=actor, request_scope=scope, manifest=manifest,
                    validate=lambda: ['source'], write_child=write), scope
            for name, actor, mode in (('journal-alice-first', 'alice', 'partial'),
                    ('journal-bob-secret', 'bob', 'full'), ('journal-alice-second', 'alice', 'full')):
                result, scope = save(name, actor, mode)
                accepted.append(result['acceptance'])
            preview, _ = save('journal-preview-only', 'alice', 'preview')
            rejected, _ = save('journal-rejected-only', 'alice', 'reject')
            assert preview['acceptance'] is None and rejected['acceptance'] is None
            authenticated.return_value = user
            scope = http._current_web_user_config_key(None)
            server, thread, base = server_for(entry)
            # setup() uses the WAL concurrency fixture; finish its physical
            # checkpoint before the GET-only witness, preserving committed data.
            with closing(sqlite3.connect(runtime.db_path)) as conn:
                assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
            logical_before = source_snapshot(runtime.db_path)
            before = hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
            wal = Path(str(runtime.db_path) + '-wal')
            wal_before = wal.read_bytes() if wal.exists() else b''
            connect = sqlite3.connect
            try:
                with patch.object(financial, 'ensure_schema', side_effect=AssertionError('GET creates schema')), \
                        patch.object(sqlite3, 'connect', side_effect=lambda *args, **kwargs: get_only_connect(connect, *args, **kwargs)):
                    args = dict(allowed_domains={financial.DOMAIN}, request_scope=scope, runtime_dir=runtime.runtime_dir)
                    one = journal.journal(runtime.db_path, limit=1, **args)
                    two = journal.journal(runtime.db_path, limit=1, page=2, **args)
                    assert one['total'] == two['total'] == 2
                    assert {r['operation_id'] for p in (one, two) for r in p['items']} == {accepted[0]['operation_id'], accepted[2]['operation_id']}
                    assert journal.journal(runtime.db_path, search='journal-bob-secret', **args)['total'] == 0
                    assert journal.journal(runtime.db_path, search='journal-alice-first', **args)['total'] == 1
                    assert journal.journal(runtime.db_path, search='journal-preview-only', **args)['total'] == 0
                    assert journal.journal(runtime.db_path, search='journal-rejected-only', **args)['total'] == 0
                    assert journal.read_acceptance(runtime.db_path, accepted[1]['operation_id'], **args) is None
                    child = accepted[0]['children'][0]['operation_id']
                    assert journal.read_acceptance(runtime.db_path, child, **args)['operation_id'] == child
                    for receipt in (accepted[0], accepted[0]['children'][0]):
                        assert receipt['journal_path'] == '/sheet-vitrina-v1/operations?operation_id=' + receipt['operation_id']
                        code, value = request(base, '/v1/sheet-vitrina-v1/operations/' + receipt['operation_id'])
                        assert code == 200 and value['operation']['operation_id'] == receipt['operation_id'], value
                    common = '/v1/sheet-vitrina-v1/operations?domain=' + financial.DOMAIN
                    code, page = request(base, common)
                    assert code == 200 and page['total'] == 2
                    assert any(r.get('partial') and len(r['children']) == 1 for r in page['items'])
                    assert not any(key in json.dumps(page) for key in ('balance_cny', 'balance_rub_value', 'proof_json', 'stored_file_path'))
                    for principal in ({**user, 'allowed_sections': ['reports']},
                            {'username': 'alice', 'role': 'supplier', 'allowed_sections': ['supply']},
                            {**user, 'username': 'other'}):
                        authenticated.return_value = principal
                        code, page = request(base, common)
                        assert code == 200 and page['total'] == 0, page
                        assert request(base, '/v1/sheet-vitrina-v1/operations/' + child)[0] == 404
                        assert request(base, '/v1/sheet-vitrina-v1/operations/' + accepted[0]['operation_id'])[0] == 404
            finally:
                stop(server, thread)
            assert hashlib.sha256(runtime.db_path.read_bytes()).hexdigest() == before
            assert (wal.read_bytes() if wal.exists() else b'') == wal_before
            assert source_snapshot(runtime.db_path) == logical_before
    print('financial common journal: native scope before count/search/page/detail, parent-only partial batches, no preview/refusal entries, exact child links, query-only and no balance disclosure: PASS')


if __name__ == '__main__': main()
