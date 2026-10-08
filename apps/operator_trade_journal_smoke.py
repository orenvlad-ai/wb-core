"""Library history in the common journal retains exact source grants and versions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_supplier_shipments_http_smoke import server_for, stop, request
from apps.operator_trade_documents_smoke import contract_bytes
from packages.application import operator_trade_documents as trade
from packages.application import operator_operations as journal
from packages.adapters import registry_upload_http_entrypoint as http


def main():
    with TemporaryDirectory(prefix='library-journal-') as directory:
        runtime, entry, _ = seed(directory)
        user = {'username': 'alice', 'role': 'operator', 'allowed_sections': ['settings']}
        with patch.object(http, '_web_auth_config', return_value={'enabled': True, 'configured': True}), \
                patch.object(http, '_authenticated_web_user') as authenticated:
            saved = []
            for actor, number in [('alice', 'Alice-original'), ('bob', 'Bob-hidden')]:
                authenticated.return_value = {**user, 'username': actor}
                scope = http._current_web_user_config_key(None)
                result = entry.handle_trade_documents_create_request(contract_bytes(), uploaded_filename='contract.xlsx',
                    fields={'request_id': 'library-journal-' + actor, 'document_type': 'contract', 'number': number},
                    actor=actor, request_scope=scope)
                saved.append(result['acceptance'])
            authenticated.return_value = user
            scope = http._current_web_user_config_key(None)
            document_id = saved[0]['source_ref']['document_id']
            edited = entry.handle_trade_documents_patch_request(document_id,
                {'request_id': 'library-journal-edit', 'number': 'Alice-updated'}, actor='alice', request_scope=scope)['acceptance']
            refused = entry.handle_trade_documents_patch_request(document_id,
                {'request_id': 'library-journal-refusal', 'amount_total': 999}, actor='alice', request_scope=scope)
            assert refused['status'] == 'rejected'
            server, thread, base = server_for(entry)
            before = runtime.db_path.read_bytes()
            try:
                with patch.object(trade, 'ensure_schema', side_effect=AssertionError('journal GET bootstrap')):
                    args = dict(allowed_domains={trade.DOMAIN}, request_scope=scope)
                    first = journal.journal(runtime.db_path, page=1, limit=1, **args)
                    second = journal.journal(runtime.db_path, page=2, limit=1, **args)
                    assert first['total'] == second['total'] == 2
                    assert {x['operation_id'] for page in (first, second) for x in page['items']} == {saved[0]['operation_id'], edited['operation_id']}
                    assert journal.journal(runtime.db_path, search='Bob-hidden', **args)['total'] == 0
                    assert journal.journal(runtime.db_path, search='Alice-original', **args)['total'] == 1
                    assert journal.journal(runtime.db_path, search='library-journal-refusal', **args)['total'] == 0
                    assert journal.journal(runtime.db_path, allowed_sections={'supply'}, request_scope=scope)['total'] == 0
                    for receipt in (saved[0], edited):
                        identity = receipt['operation_id']
                        assert receipt['journal_path'].endswith('?operation_id=' + identity)
                        code, value = request(base, '/v1/sheet-vitrina-v1/operations/' + identity)
                        assert code == 200 and value['operation']['source_ref'] == receipt['source_ref'], value
                        assert value['operation']['processing']['cost_applicable'] is False
                        assert journal.read_acceptance(runtime.db_path, identity, domain='supplier_shipment', **args) is None
                    path = '/v1/sheet-vitrina-v1/operations?domain=' + trade.DOMAIN
                    code, value = request(base, path)
                    assert code == 200 and value['total'] == 2, value
                    assert not any(secret in json.dumps(value) for secret in ('file_path', 'source_json', 'before_json', 'amount_total'))
                    for principal in ({**user, 'username': 'foreign'}, {**user, 'allowed_sections': ['supply']},
                            {**user, 'allowed_sections': ['reports']}, {'username': 'alice', 'role': 'supplier'}):
                        authenticated.return_value = principal
                        code, value = request(base, path)
                        assert code == 200 and value['total'] == 0, (principal, code, value)
                        assert request(base, '/v1/sheet-vitrina-v1/operations/' + saved[0]['operation_id'])[0] == 404
            finally:
                stop(server, thread)
            assert runtime.db_path.read_bytes() == before
    print('Library common journal: exact retained versions; native settings grant and scope before count/search/page/detail; no private data or GET writes: PASS')


if __name__ == '__main__':
    main()
