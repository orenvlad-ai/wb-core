"""Disposable native/HTTP tests for compatibility upload acknowledgements."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, INPUT_BUNDLE_FIXTURE
from packages.application import operator_compat_uploads as receipts, operator_operations as operations
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.adapters import registry_upload_http_entrypoint as http
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

NOW = '2026-10-08T13:00:00Z'


def cost(version='operator-fixture', price=123.45):
    return {'dataset_version': version, 'uploaded_at': NOW,
        'cost_price_rows': [{'group': 'Clean', 'cost_price_rub': price, 'effective_from': '2026-10-01'}]}


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(self.tmp.name))

    def save(self, version='operator-fixture', price=123.45):
        return self.rt.ingest_cost_price_payload(cost(version, price), activated_at=NOW, operator_actor='api-user')

    def test_source_and_receipt_survive_restart_with_exact_old_version(self):
        self.save(); self.save('new-version', 200)
        self.rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(self.tmp.name))
        ack = receipts.read_version(self.rt.db_path, 'cost_price_upload', 'operator-fixture')
        self.assertTrue(ack['durable_saved']); self.assertFalse(ack['calculation_completed'])
        self.assertEqual(ack['consumer_status'], 'not_tracked')
        self.assertEqual(self.rt.load_cost_price_current_state().dataset_version, 'new-version')
        self.assertEqual(self.save().status, 'rejected')
        self.assertEqual(receipts.read_version(self.rt.db_path, 'cost_price_upload', 'operator-fixture'), ack)

    def test_atomic_rollback_after_primary_rows_before_receipt(self):
        self.save()
        with patch.object(receipts, 'record', side_effect=RuntimeError('receipt failure')):
            with self.assertRaisesRegex(RuntimeError, 'receipt failure'): self.save('failed-version')
        self.assertEqual(self.rt.load_cost_price_current_state().dataset_version, 'operator-fixture')
        self.assertEqual(self.rt.list_cost_price_dataset_versions(), ['operator-fixture'])
        self.assertIsNone(receipts.read_version(self.rt.db_path, 'cost_price_upload', 'failed-version'))

    def test_concurrent_duplicate_version_has_only_one_source_and_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.save(), range(2)))
        self.assertEqual(sorted(item.status for item in results), ['accepted', 'rejected'])
        with closing(sqlite3.connect(self.rt.db_path)) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0], 1)
        self.assertEqual(self.rt.list_cost_price_dataset_versions(), ['operator-fixture'])

    def test_native_legacy_result_is_not_retroactively_acknowledged(self):
        self.rt.ingest_cost_price_payload(cost(), activated_at=NOW)
        before = hashlib.sha256(self.rt.db_path.read_bytes()).hexdigest()
        self.assertIsNone(receipts.read_version(self.rt.db_path, 'cost_price_upload', 'operator-fixture'))
        self.assertEqual(hashlib.sha256(self.rt.db_path.read_bytes()).hexdigest(), before)
        self.assertEqual(self.save().status, 'rejected')
        self.assertIsNone(receipts.read_version(self.rt.db_path, 'cost_price_upload', 'operator-fixture'))

    def test_bundle_and_cost_domains_do_not_share_identity_or_source(self):
        bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text()); bundle['bundle_version'] = 'operator-fixture'
        result = self.rt.ingest_bundle(bundle, activated_at=NOW, operator_actor='api-user')
        self.assertEqual(result.status, 'accepted')
        self.save()
        a = receipts.read_version(self.rt.db_path, 'registry_bundle_upload', 'operator-fixture')
        b = receipts.read_version(self.rt.db_path, 'cost_price_upload', 'operator-fixture')
        self.assertNotEqual(a['operation_id'], b['operation_id'])
        self.assertNotEqual(a['source_ref']['fingerprint'], b['source_ref']['fingerprint'])
        self.assertEqual(self.rt.load_persisted_upload_result('operator-fixture'), result)

    def test_receipt_immutable_and_journal_filters_before_rows_counts_search(self):
        self.save(); ack = receipts.read_version(self.rt.db_path, 'cost_price_upload', 'operator-fixture')
        with closing(sqlite3.connect(self.rt.db_path)) as conn:
            for sql in (f'DELETE FROM {receipts.TABLE}', f"UPDATE {receipts.TABLE} SET actor='other'"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'): conn.execute(sql)
                conn.rollback()
        before = hashlib.sha256(self.rt.db_path.read_bytes()).hexdigest()
        self.assertEqual(operations.journal(self.rt.db_path, allowed_domains={'ff_pool_document'}, search='operator-fixture')['total'], 0)
        self.assertIsNone(operations.read_acceptance(self.rt.db_path, ack['operation_id'], allowed_domains={'ff_pool_document'}))
        page = operations.journal(self.rt.db_path, allowed_domains=receipts.DOMAINS, search='operator-fixture')
        self.assertEqual(page['total'], 1); self.assertEqual(page['items'][0], ack)
        self.assertNotIn('123.45', json.dumps(page))
        self.assertEqual(hashlib.sha256(self.rt.db_path.read_bytes()).hexdigest(), before)

    def test_actual_http_post_once_lost_reply_exact_get_permissions_and_rejection(self):
        config = RegistryUploadHttpEntrypointConfig(host='127.0.0.1', port=0,
            upload_path=http.DEFAULT_UPLOAD_PATH, cost_price_upload_path=http.DEFAULT_COST_PRICE_UPLOAD_PATH,
            sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH, sheet_refresh_path=http.DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH, sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=self.rt.runtime_dir)
        app = RegistryUploadHttpEntrypoint(runtime_dir=self.rt.runtime_dir, runtime=self.rt,
            activated_at_factory=lambda: NOW)
        server = http.build_registry_upload_http_server(config, entrypoint=app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        def req(path, payload=None):
            request = Request(base+path, data=json.dumps(payload).encode() if payload is not None else None,
                headers={'Content-Type': 'application/json'})
            try:
                with urlopen(request) as response: return response.status, json.load(response)
            except HTTPError as exc: return exc.code, json.load(exc)
        try:
            path = http.DEFAULT_COST_PRICE_UPLOAD_PATH
            status, body = req(path, cost()); self.assertEqual(status, 200)
            ack = body['acceptance']; self.assertTrue(ack['durable_saved'])
            # Discard the POST response. Its caller retained the source version before sending.
            status, recovered = req(path+'?'+urlencode({'dataset_version': 'operator-fixture'}))
            self.assertEqual(status, 200); self.assertEqual(recovered['operation'], ack)
            self.assertEqual(req(path, cost())[0], 409)
            self.assertEqual(req(path+'?dataset_version=unknown')[0], 404)
            self.assertEqual(req(path+'?dataset_version=x&dataset_version=y')[0], 422)
            bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text()); bundle['bundle_version'] = 'operator-fixture'
            status, bundle_response = req(http.DEFAULT_UPLOAD_PATH, bundle)
            self.assertEqual(status, 200)
            status, bundle_read = req(http.DEFAULT_UPLOAD_PATH+'?bundle_version=operator-fixture')
            self.assertEqual(status, 200); self.assertEqual(bundle_read['operation'], bundle_response['acceptance'])
            self.assertNotEqual(bundle_read['operation']['operation_id'], ack['operation_id'])
            source_role = {'username': 'supplier', 'role': 'supplier', 'allowed_sections': ['supply']}
            with patch.object(http, '_web_auth_config', return_value={'configured': True, 'enabled': True}), \
                 patch.object(http, '_authenticated_web_user', return_value=source_role):
                self.assertEqual(req(path+'?dataset_version=operator-fixture')[0], 403)
                # The shared journal hides records outside the source grant.
                status, hidden = req('/v1/sheet-vitrina-v1/operations/'+ack['operation_id'])
                self.assertEqual((status, hidden), (404, {'code': 'operation_not_found'}))
                status, listing = req('/v1/sheet-vitrina-v1/operations?domain=cost_price_upload')
                self.assertEqual(status, 200)
                self.assertEqual((listing['total'], listing['items']), (0, []))
            self.assertEqual(self.rt.list_cost_price_dataset_versions(), ['operator-fixture'])
        finally:
            server.shutdown(); thread.join(timeout=5); server.server_close()


if __name__ == '__main__': unittest.main()
