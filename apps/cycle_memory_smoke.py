"""Offline operand lifetime at the durable Ready / History boundary."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from weakref import WeakKeyDictionary, ref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import sheet_vitrina_v1_cycle_smoke as fixture
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.sheet_vitrina_v1_cycle import run_cycle
from packages.application.sheet_vitrina_v1_cycle_sources import CollectedLivePlanSources
from packages.application import cycle_memory
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry


class Payload:
    def __init__(self):
        self.rows = bytearray(b'x') * (1024 * 1024)
        self.cycle = self  # Refcount release alone cannot reclaim this operand.


class MemoryEntry(fixture.CycleFake):
    def __init__(self, root, test):
        super().__init__(root)
        self.test = test
        self.clock = fixture.NOW
        self.now_factory = lambda: self.clock
        self.handle = None
        self.references = []
        self.collections = WeakKeyDictionary()
        self.sheet_plan_block.collected_source_summary = lambda handle: {
            **fixture.summary(), 'business_date': self.clock.date().isoformat()}

    def collect(self, **kwargs):
        self.mark('api_sources')
        handle, payload = CollectedLivePlanSources(), Payload()
        self.collections[handle] = SimpleNamespace(sources=payload)
        self.references.append((ref(handle), ref(payload)))
        return handle

    def derive(self, handle):
        self.test.assertIn(handle, self.collections)
        self.test.assertIsNotNone(self.references[-1][1]())
        self.events.append('derive')
        return 'plan'

    def _cycle_history(self, *args, **kwargs):
        self.test.assertEqual(len(self.collections), 0)
        self.test.assertTrue(all(h() is None and p() is None for h, p in self.references))
        return super()._cycle_history(*args, **kwargs)


class CycleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CycleTests('test_recovery_date_bounds_and_fresh_acceptance')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.entry = MemoryEntry(self.fixture.root, self)
        self.receipt, lease = self.fixture.accepted()
        self.addCleanup(lease.close)

    def run_owned(self):
        with heavy_admitted(self.fixture.root, operation='cycle'):
            return run_cycle(self.entry, self.fixture.store, self.receipt,
                             self.fixture.config, lambda _: None)

    def test_payload_released_after_durable_ready_before_history(self):
        original = self.fixture.store.write
        observed = []
        def write(value):
            if value['current_stage'] == 'final_ready' and value['stages'][-2]['status'] == 'complete':
                self.assertIsNotNone(self.entry.references[-1][1]())
                original(value)
                self.assertEqual(self.fixture.store.read(value['cycle_id'])['stages'][-2]['status'], 'complete')
                self.assertIsNotNone(self.entry.references[-1][1]())
                observed.append(True)
            else:
                original(value)
        with patch.object(self.fixture.store, 'write', side_effect=write):
            self.assertEqual(self.run_owned()['status'], 'complete')
        self.assertEqual(observed, [True])
        persisted = self.fixture.store.read(self.receipt['cycle_id'])
        self.assertIn('gc_collected', persisted['history_memory_release'])
        self.assertNotIn('gc_collected', persisted['final_versions'])

    def test_failed_durable_ready_write_does_not_enter_release_or_history(self):
        original = self.fixture.store.write
        failed = False
        def write(value):
            nonlocal failed
            if not failed and value['current_stage'] == 'final_ready' and value['stages'][-2]['status'] == 'complete':
                failed = True
                raise OSError('offline receipt write failed')
            original(value)
        with patch.object(self.fixture.store, 'write', side_effect=write), \
             patch.object(cycle_memory, 'release_cycle_memory', wraps=cycle_memory.release_cycle_memory) as release:
            with self.assertRaises(OSError):
                self.run_owned()
        release.assert_not_called()
        self.assertNotIn('rolling14', self.entry.events)

    def test_rollover_after_ready_creates_fresh_collection(self):
        original = self.fixture.store.write
        crossed = False
        def write(value):
            nonlocal crossed
            original(value)
            if not crossed and value['current_stage'] == 'final_ready' and value['stages'][-2]['status'] == 'complete':
                crossed = True
                self.entry.clock += timedelta(days=1)
        with patch.object(self.fixture.store, 'write', side_effect=write):
            result = self.run_owned()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.entry.references), 2)
        self.assertEqual(self.entry.events.count('api_sources'), 2)
        self.assertEqual(self.entry.events.count('rolling14'), 1)

    def test_late_ready_operands_reclaimed_and_recorded_before_child(self):
        self.entry._cycle_history = MethodType(Entry._cycle_history, self.entry)
        references = []
        def verify(_ready):
            payload = Payload()
            references.append(ref(payload))
        self.entry._cycle_verify_ready = verify
        def build(**kwargs):
            self.assertEqual(len(references), 1)
            self.assertIsNone(references[0]())
            saved = self.fixture.store.read(self.receipt['cycle_id'])
            self.assertEqual(saved['current_stage'], 'rolling14')
            self.assertEqual(saved['stages'][-1]['status'], 'running')
            self.assertIn('gc_collected', saved['history_memory_before_child'])
            return {'status': 'published'}
        with patch.object(Entry, '_cycle_owned_worker_identity', return_value={'job_id': 'offline'}), \
             patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history', side_effect=build):
            self.assertEqual(self.run_owned()['status'], 'complete')

    def test_failed_pre_child_telemetry_write_prevents_child_start(self):
        self.entry._cycle_history = MethodType(Entry._cycle_history, self.entry)
        self.entry._cycle_verify_ready = lambda _: None
        original = self.fixture.store.write
        failed = False
        def write(value):
            nonlocal failed
            if not failed and 'history_memory_before_child' in value:
                failed = True
                raise OSError('offline pre-child receipt write failed')
            original(value)
        with patch.object(Entry, '_cycle_owned_worker_identity', return_value={'job_id': 'offline'}), \
             patch.object(self.fixture.store, 'write', side_effect=write), \
             patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history') as build:
            with self.assertRaises(OSError):
                self.run_owned()
        build.assert_not_called()


class AllocatorTests(unittest.TestCase):
    def test_non_linux_does_not_load_native_library(self):
        with patch.object(cycle_memory.sys, 'platform', 'darwin'), patch('ctypes.CDLL') as library:
            result = cycle_memory.release_cycle_memory()
        self.assertEqual(result['allocator_trim'], 'unsupported_platform')
        self.assertNotIn('rss_before_kib', result)
        library.assert_not_called()

    def test_fixed_glibc_call_signature_and_rss_telemetry(self):
        import ctypes
        trim = Mock(return_value=1)
        library = SimpleNamespace(gnu_get_libc_version=object(), malloc_trim=trim)
        with patch.object(cycle_memory.sys, 'platform', 'linux'), \
             patch('ctypes.CDLL', return_value=library) as loader, \
             patch.object(cycle_memory, '_rss_kib', side_effect=[500000, 300000]):
            result = cycle_memory.release_cycle_memory()
        loader.assert_called_once_with(None)
        trim.assert_called_once_with(0)
        self.assertEqual(trim.argtypes, [ctypes.c_size_t])
        self.assertIs(trim.restype, ctypes.c_int)
        self.assertEqual(result['allocator_trim'], 'released')
        self.assertEqual((result['rss_before_kib'], result['rss_after_kib']), (500000, 300000))

    def test_missing_glibc_or_loader_failure_is_nonfatal(self):
        for library in (SimpleNamespace(), OSError('offline unavailable')):
            with self.subTest(library=type(library).__name__), \
                 patch.object(cycle_memory.sys, 'platform', 'linux'), \
                 patch('ctypes.CDLL', **({'side_effect': library} if isinstance(library, OSError)
                                        else {'return_value': library})):
                self.assertEqual(cycle_memory.release_cycle_memory()['allocator_trim'], 'unavailable')

    def test_native_no_release_is_not_a_failure(self):
        library = SimpleNamespace(gnu_get_libc_version=object(), malloc_trim=Mock(return_value=0))
        with patch.object(cycle_memory.sys, 'platform', 'linux'), patch('ctypes.CDLL', return_value=library):
            self.assertEqual(cycle_memory.release_cycle_memory()['allocator_trim'], 'no_release')

    @unittest.skipUnless(sys.platform == 'linux', 'native allocator measurement requires Linux')
    def test_native_reclamation_preserves_live_bytes(self):
        survivor = bytearray(b'live-cycle-evidence' * 4096)
        payloads = [Payload() for _ in range(32)]
        references = [ref(payload) for payload in payloads]
        del payloads
        measured = cycle_memory.release_cycle_memory()
        self.assertTrue(all(reference() is None for reference in references))
        self.assertEqual(survivor, bytearray(b'live-cycle-evidence' * 4096))
        self.assertIn(measured['allocator_trim'], {'released', 'no_release', 'unavailable'})
        # A synthetic Linux observation, not an end-to-end production saving.
        print('cycle_memory_synthetic_32mib ' + json.dumps(measured, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
