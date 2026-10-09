"""Same native financial tables retain distinct principal-scoped journal domains."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import json
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
            before = hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
            try:
                with patch.object(financial, 'ensure_schema', side_effect=AssertionError('GET creates schema')):
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
    print('CNY common journal: exact native action and principal before count/search/page/detail; mixed families, child links, no account balance, GET-only: PASS')


if __name__ == '__main__':
    main()
