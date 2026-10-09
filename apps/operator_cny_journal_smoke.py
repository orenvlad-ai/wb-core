"""Same native financial tables retain distinct principal-scoped journal domains."""
from pathlib import Path
from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import json
import sqlite3
from urllib.parse import parse_qs, urlparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup, document
from apps.operator_supplier_shipments_http_smoke import server_for, stop, request
from packages.application import operator_cny_documents as cny
from packages.application import operator_supplier_financial as financial
from packages.application import operator_operations as journal
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.adapters import registry_upload_http_entrypoint as http


def source_snapshot(db_path):
    """Whole committed fixture schema and typed rows, independent of WAL layout."""
    def typed(value):
        if value is None: return ['null']
        if isinstance(value, int): return ['integer', str(value)]
        if isinstance(value, float): return ['real', value.hex()]
        if isinstance(value, str): return ['text', value]
        if isinstance(value, bytes): return ['blob', value.hex()]
        raise AssertionError('unknown SQLite value type')
    with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        schema = list(conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
        tables = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = [[typed(value) for value in row] for row in conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]
            tables[name] = sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False))
        return schema, tables


def get_only_connect(connect, *args, **kwargs):
    """Reject a source writer open and deny every mutating SQLite action on GET."""
    database = args[0] if args else kwargs['database']
    assert kwargs.get('uri') is True and parse_qs(urlparse(str(database)).query).get('mode') == ['ro'], 'GET opens a source writer'
    conn = connect(*args, **kwargs)
    conn.execute('PRAGMA query_only=ON')
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    def authorize(action, one, two, database, trigger):
        if action == sqlite3.SQLITE_PRAGMA:
            if one == 'query_only': return sqlite3.SQLITE_OK if two is None or str(two).upper() in ('ON', '1') else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if one in {'table_info', 'table_xinfo', 'foreign_key_list', 'foreign_key_check', 'index_list', 'index_info', 'schema_version', 'data_version', 'integrity_check'} else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_ATTACH:
            return sqlite3.SQLITE_OK if parse_qs(urlparse(str(one)).query).get('mode') == ['ro'] else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE} else sqlite3.SQLITE_DENY
    conn.set_authorizer(authorize)
    return conn


def main():
    with TemporaryDirectory(prefix='cny-common-journal-') as directory:
        runtime, ledger = setup(directory)
        entry = RegistryUploadHttpEntrypoint(runtime=runtime, runtime_dir=runtime.runtime_dir)
        user = {'username': 'alice', 'role': 'operator', 'allowed_sections': ['supply']}
        with patch.object(http, '_web_auth_config', return_value={'enabled': True, 'configured': True}), \
                patch.object(http, '_authenticated_web_user') as authenticated:
            receipts = []
            for actor, identity, amount in [('alice', 'journal-cny-alice-one', '1300'),
                    ('bob', 'journal-cny-bob-hidden', '1400'), ('alice', 'journal-cny-alice-two', '1500')]:
                authenticated.return_value = {**user, 'username': actor}
                scope = http._current_web_user_config_key(None)
                payload = {'request_id': identity, 'operation_date': '2026-07-24',
                           'cny_amount': '100', 'rub_value': amount}
                result = cny.execute(ledger, action='cny_opening', payload=payload,
                    request_scope=scope, actor=actor, native_write=lambda: ledger.create_opening_balance(payload))
                receipts.append(result['acceptance'])
            authenticated.return_value = user
            scope = http._current_web_user_config_key(None)
            expense = financial.execute(runtime, action='confirm_upload', payload={'request_id': 'journal-cny-expense'},
                shipment_id='source', actor='alice', request_scope=scope,
                manifest=[{'child_key': 'expense', 'kind': 'financial', 'subject_id': 'journal-cny-expense'}],
                validate=lambda: ['source'], write_child=lambda child: document(runtime, child))['acceptance']
            rejected = cny.execute(ledger, action='cny_opening', payload={'request_id': 'journal-cny-rejected'},
                request_scope=scope, actor='alice', native_write=lambda: ledger.create_opening_balance({'operation_date': 'bad'}))
            assert rejected['acceptance'] is None
            server, thread, base = server_for(entry)
            # setup() uses a WAL concurrency fixture; normalize its committed pages
            # before the physical GET-only witness, not during the observed reads.
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
                    args = dict(allowed_domains={cny.DOMAIN, financial.DOMAIN}, request_scope=scope,
                                runtime_dir=runtime.runtime_dir)
                    pages = [journal.journal(runtime.db_path, page=n, limit=1, **args) for n in (1, 2, 3)]
                    assert all(p['total'] == 3 for p in pages), pages
                    assert {row['operation_id'] for p in pages for row in p['items']} == {
                        receipts[0]['operation_id'], receipts[2]['operation_id'], expense['operation_id']}
                    assert journal.journal(runtime.db_path, domain=cny.DOMAIN, **args)['total'] == 2
                    assert journal.journal(runtime.db_path, domain=financial.DOMAIN, **args)['total'] == 1
                    for identity in ('journal-cny-bob-hidden', 'journal-cny-rejected'):
                        assert journal.journal(runtime.db_path, search=identity, **args)['total'] == 0
                    for receipt in (receipts[0], receipts[0]['children'][0]):
                        identity = receipt['operation_id']
                        assert receipt['journal_path'] == '/sheet-vitrina-v1/operations?operation_id=' + identity
                        assert journal.read_acceptance(runtime.db_path, identity, domain=financial.DOMAIN, **args) is None
                        assert journal.read_acceptance(runtime.db_path, identity, domain=cny.DOMAIN, **args)['domain'] == cny.DOMAIN
                        code, value = request(base, '/v1/sheet-vitrina-v1/operations/' + identity)
                        assert code == 200 and value['operation']['domain'] == cny.DOMAIN, value
                    common = '/v1/sheet-vitrina-v1/operations?domain=' + cny.DOMAIN
                    code, page = request(base, common)
                    assert code == 200 and page['total'] == 2, page
                    assert all(r['domain'] == cny.DOMAIN for r in page['items'])
                    assert not any(key in json.dumps(page) for key in ('balance_cny', 'balance_rub_value', 'proof_json', 'stored_file_path'))
                    for principal in ({**user, 'allowed_sections': ['reports']},
                            {'username': 'alice', 'role': 'supplier', 'allowed_sections': ['supply']},
                            {**user, 'username': 'other'}):
                        authenticated.return_value = principal
                        code, page = request(base, common)
                        assert code == 200 and page['total'] == 0, page
                        for receipt in (receipts[0], receipts[0]['children'][0]):
                            assert request(base, '/v1/sheet-vitrina-v1/operations/' + receipt['operation_id'])[0] == 404
            finally:
                stop(server, thread)
            assert hashlib.sha256(runtime.db_path.read_bytes()).hexdigest() == before
            assert (wal.read_bytes() if wal.exists() else b'') == wal_before
            assert source_snapshot(runtime.db_path) == logical_before
    print('CNY common journal: exact native action and principal before count/search/page/detail; mixed families, child links, no account balance, GET-only: PASS')


if __name__ == '__main__':
    main()
