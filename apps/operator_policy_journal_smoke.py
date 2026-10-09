"""Common policy journal preserves native grants and actor isolation before aggregation."""
from datetime import datetime, timezone
from contextlib import closing
import sqlite3
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
from apps.warehouse_fbs_material_rematerialization_smoke import _seed, DAY, NOW
from apps.operator_cny_journal_smoke import source_snapshot, get_only_connect
from packages.application import operator_policy as policy, operator_operations as journal
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.adapters import registry_upload_http_entrypoint as http
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = _seed(Path(self.tmp.name), mixed=False)
        self.entry = RegistryUploadHttpEntrypoint(runtime_dir=self.runtime.runtime_dir,
            runtime=self.runtime, activated_at_factory=lambda: NOW,
            now_factory=lambda: datetime(2026, 8, 26, 12, tzinfo=timezone.utc))
        legacy = {'effective_date': DAY, 'buyout_rate': '1', 'tax_rate': '0.1234'}
        legacy['preview_fingerprint'] = self.entry.calculation_parameters_block.preview_version(legacy)['preview_fingerprint']
        v4 = {'tax_rate': '0.4321'}
        v4['preview_fingerprint'] = self.entry.proxy_v4_parameters_block.preview_tax_version(v4)['preview_fingerprint']
        incident = {'base_revision': 0, 'active': False, 'excluded_wb_warehouse_ids': [],
            'reason': 'policy journal own incident', 'effective_from': DAY, 'status': 'disabled'}
        self.acks = []
        for index, (kind, actor, payload) in enumerate((('legacy_proxy', 'alice', legacy),
                ('proxy_v4_tax', 'bob', v4), ('wb_incident_policy', 'alice', incident)), 1):
            self.acks.append(policy.accept(self.entry, kind, payload, actor=actor,
                operation_id='oppolicy_' + format(index, '032x'))['acceptance'])

    def test_actor_and_kind_filters_precede_counts_search_pages_and_detail(self):
        # Normalize the disposable WAL before the existing physical RO witness.
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
        logical_before = source_snapshot(self.runtime.db_path)
        wal = Path(str(self.runtime.db_path) + '-wal')
        wal_before = wal.read_bytes() if wal.exists() else b''
        before = hashlib.sha256(self.runtime.db_path.read_bytes()).hexdigest()
        connect = sqlite3.connect
        with patch.object(sqlite3, 'connect', side_effect=lambda *args, **kwargs: get_only_connect(connect, *args, **kwargs)):
            selected = set(policy.KINDS)
            first = journal.journal(self.runtime.db_path, allowed_domains=selected, actor='alice', limit=1)
            second = journal.journal(self.runtime.db_path, allowed_domains=selected, actor='alice', limit=1, page=2)
            self.assertEqual((first['total'], second['total']), (2, 2))
            self.assertEqual({p['items'][0]['operation_id'] for p in (first, second)},
                {self.acks[0]['operation_id'], self.acks[2]['operation_id']})
            self.assertEqual(journal.journal(self.runtime.db_path, allowed_domains=selected,
                actor='alice', search='0.4321')['total'], 0)
            self.assertEqual(journal.journal(self.runtime.db_path, allowed_domains={'legacy_proxy'},
                actor='alice', search='policy journal own incident')['total'], 0)
            self.assertIsNone(journal.read_acceptance(self.runtime.db_path, self.acks[1]['operation_id'],
                allowed_domains=selected, actor='alice'))
            self.assertIsNone(journal.read_acceptance(self.runtime.db_path, self.acks[2]['operation_id'],
                allowed_domains={'legacy_proxy'}, actor='alice'))
            self.assertEqual(journal.journal(self.runtime.db_path, allowed_domains=selected)['total'], 0)
            for ack in self.acks:
                self.assertEqual(ack['journal_path'], '/sheet-vitrina-v1/operations?operation_id=' + ack['operation_id'])
                self.assertIn('policy_operation_id=', ack['source_path'])
        self.assertEqual(hashlib.sha256(self.runtime.db_path.read_bytes()).hexdigest(), before)
        self.assertEqual(wal.read_bytes() if wal.exists() else b'', wal_before)
        self.assertEqual(source_snapshot(self.runtime.db_path), logical_before)

    def test_actual_http_native_and_common_grants_are_equal(self):
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
        # Normalize the disposable WAL before the existing physical RO witness.
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
        logical_before = source_snapshot(self.runtime.db_path)
        wal = Path(str(self.runtime.db_path) + '-wal')
        wal_before = wal.read_bytes() if wal.exists() else b''
        before = hashlib.sha256(self.runtime.db_path.read_bytes()).hexdigest()
        connect = sqlite3.connect
        try:
            with patch.object(http, '_web_auth_config', return_value={'enabled': True, 'configured': True}), \
                 patch.object(http, '_authenticated_web_user') as authenticated, \
                  patch.object(sqlite3, 'connect', side_effect=lambda *args, **kwargs: get_only_connect(connect, *args, **kwargs)):
                for actor, role, sections, visible in (
                    ('alice', 'operator', ['settings'], {0}),
                    ('alice', 'supply_operator', ['supply'], {2}),
                    ('alice', 'operator', ['settings', 'supply'], {0, 2}),
                    ('bob', 'operator', ['settings'], {1}),
                    ('bob', 'supply_operator', ['supply'], set()),
                    ('alice', 'operator', ['reports'], set()),
                    ('alice', 'supplier', ['settings', 'supply'], set()),
                ):
                    user = {'username': actor, 'role': role, 'allowed_sections': sections}
                    authenticated.return_value = user
                    status, page = get('/v1/sheet-vitrina-v1/operations?limit=1')
                    self.assertEqual((status, page['total']), (200, len(visible)), user)
                    seen = {item['operation_id'] for item in page['items']}
                    if page['has_more']:
                        seen.update(item['operation_id'] for item in get('/v1/sheet-vitrina-v1/operations?limit=1&page=2')[1]['items'])
                    self.assertEqual(seen, {self.acks[i]['operation_id'] for i in visible}, user)
                    for index, ack in enumerate(self.acks):
                        identity = ack['operation_id']; kind = ack['domain']
                        native_path = policy.PATH + kind + '/' + identity
                        granted = http._user_can_access_path(user, native_path)
                        self.assertEqual(kind in {item['domain'] for item in page['available_domains']}, granted)
                        self.assertEqual(get(native_path)[0], 200 if index in visible else 404 if granted else 403)
                        self.assertEqual(get('/v1/sheet-vitrina-v1/operations/' + identity)[0], 200 if index in visible else 404)
                        result = get('/v1/sheet-vitrina-v1/operations?domain=' + kind)[1]
                        self.assertEqual(result['total'], int(index in visible))
        finally:
            server.shutdown(); worker.join(timeout=5); server.server_close()
        self.assertEqual(hashlib.sha256(self.runtime.db_path.read_bytes()).hexdigest(), before)
        self.assertEqual(wal.read_bytes() if wal.exists() else b'', wal_before)
        self.assertEqual(source_snapshot(self.runtime.db_path), logical_before)


if __name__ == '__main__': unittest.main()
