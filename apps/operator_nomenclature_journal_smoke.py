"""Common journal keeps the native SKU grant and actor boundary; local data only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.nomenclature_activation_intents_smoke import fixture
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application import operator_nomenclature as sku, operator_operations as journal
from packages.adapters import registry_upload_http_entrypoint as http
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = fixture(Path(self.tmp.name), facilities=0)
        self.entry = RegistryUploadHttpEntrypoint(runtime_dir=self.runtime.runtime_dir,
            runtime=self.runtime, activated_at_factory=lambda: '2026-10-08T13:00:00Z')
        self.acks = []
        for index, actor in enumerate(('alice', 'bob', 'alice'), 1):
            payload = {'_operator_request_id': 'opsku_' + format(index, '032x'),
                'group_key': 'journal_' + str(index), 'label': 'Synthetic search ' + str(index)}
            self.acks.append(self.entry.handle_sku_groups_create_request(payload, actor=actor)['acceptance'])

    def test_actor_filter_precedes_total_pagination_search_and_detail(self):
        before = hashlib.sha256(self.runtime.db_path.read_bytes()).hexdigest()
        first = journal.journal(self.runtime.db_path, allowed_domains={sku.DOMAIN}, actor='alice', limit=1)
        second = journal.journal(self.runtime.db_path, allowed_domains={sku.DOMAIN}, actor='alice', limit=1, page=2)
        self.assertEqual((first['total'], second['total']), (2, 2))
        self.assertEqual({p['items'][0]['operation_id'] for p in (first, second)},
                         {self.acks[0]['operation_id'], self.acks[2]['operation_id']})
        self.assertEqual(journal.journal(self.runtime.db_path, allowed_domains={sku.DOMAIN},
            actor='alice', search='journal_2')['total'], 0)
        self.assertIsNone(journal.read_acceptance(self.runtime.db_path, self.acks[1]['operation_id'],
            allowed_domains={sku.DOMAIN}, actor='alice'))
        self.assertEqual(journal.journal(self.runtime.db_path, allowed_domains={sku.DOMAIN})['total'], 0)
        self.assertEqual(journal.journal(self.runtime.db_path, allowed_domains=set(), actor='alice')['total'], 0)
        for ack in self.acks:
            self.assertEqual(ack['journal_path'], '/sheet-vitrina-v1/operations?operation_id=' + ack['operation_id'])
        self.assertEqual(hashlib.sha256(self.runtime.db_path.read_bytes()).hexdigest(), before)

    def test_real_http_same_grants_and_actor_as_native_detail(self):
        config = RegistryUploadHttpEntrypointConfig(host='127.0.0.1', port=0,
            upload_path=http.DEFAULT_UPLOAD_PATH, sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=http.DEFAULT_SHEET_REFRESH_PATH, sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH, runtime_dir=self.runtime.runtime_dir)
        server = http.build_registry_upload_http_server(config, entrypoint=self.entry)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        def get(path):
            try:
                with urlopen(base + path) as response: return response.status, json.load(response)
            except HTTPError as exc: return exc.code, json.load(exc)
        try:
            with patch.object(http, '_web_auth_config', return_value={'enabled': True, 'configured': True}), \
                 patch.object(http, '_authenticated_web_user') as authenticated:
                authenticated.return_value = {'username': 'alice', 'role': 'operator', 'allowed_sections': ['settings']}
                status, page = get('/v1/sheet-vitrina-v1/operations?domain=nomenclature&limit=1')
                self.assertEqual(status, 200); self.assertEqual(page['total'], 2)
                for index, expected in ((0, 200), (1, 404)):
                    identity = self.acks[index]['operation_id']
                    self.assertEqual(get(sku.REQUEST_PATH + identity)[0], expected)
                    status, result = get('/v1/sheet-vitrina-v1/operations/' + identity)
                    self.assertEqual(status, expected)
                    if status == 200:
                        self.assertEqual(result['operation']['domain'], sku.DOMAIN)
                        self.assertEqual(result['operation']['actor'], 'alice')
                authenticated.return_value = {'username': 'alice', 'role': 'operator', 'allowed_sections': ['reports']}
                status, page = get('/v1/sheet-vitrina-v1/operations?domain=nomenclature')
                self.assertEqual(status, 200); self.assertEqual(page['total'], 0)
                self.assertNotIn(sku.DOMAIN, [item['domain'] for item in page['available_domains']])
                self.assertEqual(get('/v1/sheet-vitrina-v1/operations/' + self.acks[0]['operation_id'])[0], 404)
                self.assertEqual(get(sku.REQUEST_PATH + self.acks[0]['operation_id'])[0], 403)
        finally:
            server.shutdown(); worker.join(timeout=5); server.server_close()


if __name__ == '__main__': unittest.main()
