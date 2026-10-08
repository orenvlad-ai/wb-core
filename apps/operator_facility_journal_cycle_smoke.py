#!/usr/bin/env python3
"""Native facility acceptance through the common journal and owned cycle drain."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_facility_mappings_smoke import fixture, envelope, submit
from packages.application import operator_facility_mappings as facility, operator_operations as journal
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.adapters import registry_upload_http_entrypoint as http


class FacilityIntegrationTests(unittest.TestCase):
    def test_common_journal_principal_search_pagination_and_source_grants(self):
        with TemporaryDirectory() as raw:
            runtime, surface = fixture(Path(raw))
            saved = submit(surface, 'create', envelope(request_id='journal-facility-0001', name='Exact warehouse', city='Москва', active=False))
            identity = saved['acceptance']['operation_id']
            before = runtime.db_path.read_bytes()
            kwargs = dict(allowed_domains={facility.DOMAIN}, request_scope='principal-A')
            with patch('packages.application.ff_pool_foundation.ensure_ff_pool_foundation_schema', side_effect=AssertionError('journal schema write')):
                result = journal.journal(runtime.db_path, **kwargs)
                self.assertEqual(result['total'], 1)
                self.assertEqual(result['items'][0]['operation_id'], identity)
                self.assertEqual(journal.journal(runtime.db_path, **kwargs, search=identity)['total'], 1)
                self.assertEqual(journal.journal(runtime.db_path, **kwargs, page=2, limit=1)['items'], [])
                exact = journal.read_acceptance(runtime.db_path, identity, **kwargs)
                self.assertEqual(exact['journal_path'], '/sheet-vitrina-v1/operations?operation_id=' + identity)
                self.assertEqual(exact['detail_path'], '/v1/sheet-vitrina-v1/operations/' + identity)
                for changes in ({'request_scope': 'foreign'}, {'supplier_safe': True}, {'allowed_domains': set()}):
                    denied = {**kwargs, **changes}
                    self.assertEqual(journal.journal(runtime.db_path, **denied, search=identity)['total'], 0)
                    self.assertIsNone(journal.read_acceptance(runtime.db_path, identity, **denied))
                with patch.object(facility, 'journal_entries', side_effect=AssertionError('grant applied after source read')):
                    self.assertEqual(journal.journal(runtime.db_path, allowed_domains={'plan_report_baseline'}, request_scope='principal-A')['total'], 0)
            self.assertEqual(runtime.db_path.read_bytes(), before)
            for section in ('reports', 'cash', 'settings', 'feedbacks'):
                self.assertNotIn(facility.DOMAIN, http._operator_domains_for_user({'username': section, 'role': 'operator', 'allowed_sections': [section]}))
            self.assertIn(facility.DOMAIN, http._operator_domains_for_user({'username': 'supply', 'role': 'operator', 'allowed_sections': ['supply']}))

    def test_facility_and_library_receipts_share_pagination_without_hidden_rows(self):
        from packages.application import operator_trade_documents as trade
        with TemporaryDirectory() as raw:
            runtime, surface = fixture(Path(raw))
            entry = Entry(runtime_dir=runtime.runtime_dir, runtime=runtime)
            expected = []
            for index in range(3):
                expected.append(submit(surface, 'create', envelope(request_id=f'mixed-facility-{index:04d}',
                    name=f'Mixed facility {index}', city='Москва', active=False))['acceptance'])
            for index, scope in enumerate(('principal-A', 'principal-A', 'foreign')):
                receipt = entry.handle_trade_documents_create_request(f'invoice-{index}'.encode(),
                    uploaded_filename=f'invoice-{index}.pdf', fields={'request_id': f'mixed-library-{index:04d}',
                    'document_type': 'invoice', 'number': f'Mixed library {index}'}, actor='fixture', request_scope=scope)['acceptance']
                if scope == 'principal-A': expected.append(receipt)
            kwargs = dict(allowed_domains={facility.DOMAIN, trade.DOMAIN}, request_scope='principal-A')
            ordered = sorted(expected, key=lambda item: (item['accepted_at'], item['operation_id']), reverse=True)
            for limit in (1, 2, 3):
                found = []
                for page in range(1, 7):
                    result = journal.journal(runtime.db_path, page=page, limit=limit, **kwargs)
                    self.assertEqual(result['total'], len(expected))
                    found.extend(item['operation_id'] for item in result['items'])
                self.assertEqual(found, [item['operation_id'] for item in ordered])
            self.assertEqual(journal.journal(runtime.db_path, search='Mixed library 2', **kwargs)['total'], 0)
            self.assertEqual(journal.journal(runtime.db_path, search=expected[0]['operation_id'], **kwargs)['total'], 1)

    def test_cycle_drain_requires_both_native_owners_and_completes_actual_activation(self):
        with TemporaryDirectory() as raw:
            runtime, surface = fixture(Path(raw))
            created = submit(surface, 'create', envelope(request_id='cycle-facility-0001', name='Cycle warehouse', city='Москва', active=False))
            fid = created['acceptance']['source_ref']['entity_id']
            detail = surface.facility_detail(fid)['facility']
            pending = submit(surface, 'activate', envelope(request_id='cycle-facility-activate', active=True,
                expected_updated_at=detail['updated_at'], expected_source_digest=detail['operator_source_digest']), fid)
            identity = pending['acceptance']['operation_id']
            self.assertEqual(pending['acceptance']['state'], 'processing')
            entry = Entry.__new__(Entry)
            entry.runtime = runtime
            before = runtime.db_path.read_bytes()
            with self.assertRaises(Exception):
                entry._cycle_drain_facility_activations('no-owner')
            with heavy_admitted(runtime.runtime_dir, operation='cycle'):
                with self.assertRaises(Exception):
                    entry._cycle_drain_facility_activations('no-owner')
                self.assertEqual(runtime.db_path.read_bytes(), before)
                with warehouse_functional_job_lock(runtime.runtime_dir) as owner:
                    with self.assertRaises(Exception):
                        entry._cycle_drain_facility_activations('foreign-owner')
                    result = entry._cycle_drain_facility_activations(owner['owner_token'])
                    self.assertEqual(result['active'], 1, result)
                    self.assertEqual(entry._cycle_drain_facility_activations(owner['owner_token'])['captured'], 0)
            exact = journal.read_acceptance(runtime.db_path, identity, allowed_domains={facility.DOMAIN}, request_scope='principal-A')
            self.assertEqual(exact['state'], 'completed')
            self.assertTrue(exact['processing']['published_active'])
            self.assertFalse(exact['calculation_completed'])
            self.assertTrue(surface.facility_detail(fid)['facility']['active'])


if __name__ == '__main__':
    unittest.main()
