#!/usr/bin/env python3
"""Bounded startup observations, with no real listener or production writes."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import search_cluster_cleaner_release_probe as probe


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.calls=[]
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(probe.time,'monotonic',side_effect=lambda:self.now))

    def sleep(self,seconds):
        self.now+=seconds

    def run_listener(self,respond,*,phase='before_complete',budget=2):
        def request(url,timeout):
            self.calls.append((url,timeout))
            return respond(url,timeout)
        with patch.object(probe.urllib.request,'urlopen',side_effect=request):
            probe._listener_ready(phase=phase,deadline=budget,sleep=self.sleep)

    def expected(self,url,timeout):
        raise urllib.error.HTTPError(url,401 if url.endswith(probe.SUMMARY_PATH) else 404,'',{},None)

    def test_refused_and_timeout_then_exact_auth_pair(self):
        failures=[urllib.error.URLError(ConnectionRefusedError()),TimeoutError()]
        def response(url,timeout):
            if failures:raise failures.pop(0)
            return self.expected(url,timeout)
        self.run_listener(response)
        self.assertEqual(len(self.calls),4)
        self.assertEqual(self.now,1)
        self.assertTrue(all(timeout<=2 for _,timeout in self.calls))

    def test_timeout_consumes_remaining_budget_without_another_request(self):
        def response(url,timeout):
            self.now+=timeout
            raise urllib.error.URLError(TimeoutError())
        with self.assertRaisesRegex(RuntimeError,'listener not ready'):
            self.run_listener(response,budget=0.2)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.now,0.2)

    def test_refused_loop_stops_at_deadline(self):
        with self.assertRaisesRegex(RuntimeError,'listener not ready'):
            self.run_listener(lambda *_: (_ for _ in ()).throw(ConnectionRefusedError()))
        self.assertEqual(self.now,2)
        self.assertEqual(len(self.calls),4)

    def test_semantic_and_other_transport_failures_are_terminal(self):
        for error in (urllib.error.HTTPError('url',503,'',{},None),
                      urllib.error.URLError('unknown'),ConnectionResetError()):
            with self.subTest(error=error):
                self.calls=[]
                with self.assertRaises(Exception):
                    self.run_listener(lambda *_, error=error: (_ for _ in ()).throw(error))
                self.assertEqual(len(self.calls),1)
                self.assertEqual(self.now,0)

    def test_after_complete_never_retries(self):
        with self.assertRaises(ConnectionRefusedError):
            self.run_listener(lambda *_: (_ for _ in ()).throw(ConnectionRefusedError()),phase='after_complete')
        self.assertEqual(len(self.calls),1)

    def test_sha_rechecked_after_startup_before_settings(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.wb-core-runtime-sha').write_text('expected')
            (root/'.wb-core-deploy.json').write_text(json.dumps(dict(commit='expected',deployment_complete=False)))
            def response(url,timeout):
                (root/'.wb-core-runtime-sha').write_text('changed')
                return self.expected(url,timeout)
            with patch.object(probe.urllib.request,'urlopen',side_effect=response), \
                    patch.object(probe,'StoreRegistry') as registry:
                with self.assertRaisesRegex(RuntimeError,'SHA or deployment phase mismatch'):
                    probe.check(phase='before_complete',expected_sha='expected',runtime_dir=root,
                        admission_dir=root,app_dir=root,sleep=self.sleep)
                registry.assert_not_called()

    def test_worker_does_not_receive_a_new_budget_after_listener_wait(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.wb-core-runtime-sha').write_text('expected')
            (root/'.wb-core-deploy.json').write_text(json.dumps(dict(commit='expected',deployment_complete=False)))
            (root/'stage-e-config.json').write_text(json.dumps(dict(seller_id='fixture',account_scope='fixture')))
            with patch.object(probe,'_listener_ready',side_effect=lambda **_:self.sleep(25)), \
                    patch.object(probe,'StoreRegistry') as registry:
                conn=registry.return_value.session.return_value.__enter__.return_value
                conn.execute.return_value.fetchone.side_effect=[
                    dict(enabled=0,baseline_ready=1,restore_hold=1,transport_enabled=0),None]
                with self.assertRaisesRegex(RuntimeError,'worker not ready'):
                    probe.check(phase='before_complete',expected_sha='expected',runtime_dir=root,
                        admission_dir=root,app_dir=root,sleep=self.sleep)
                self.assertEqual(self.now,25)


if __name__=='__main__':unittest.main()
