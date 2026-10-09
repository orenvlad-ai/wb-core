#!/usr/bin/env python3
"""Native JSON store/worker; temporary inventory and fake provider/thread only."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import sheet_vitrina_v1_feedbacks_complaints as source


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.store = source.JsonFileFeedbacksComplaintsSubmitJobStore(
            self.runtime, journal=source.JsonFileFeedbacksComplaintJournal(self.runtime))
        self.payload = dict(request_key='exact-request', account_id='fixture-account',
            feedback_ids=[str(i) + '🙂' * 155 for i in range(20)], max_submit=5,
            date_from='2026-07-01', date_to='2026-07-02', stars=[1,2,3,4,5])
        self.threads = []

    def thread(self, _runtime, **kwargs):
        self.threads.append(kwargs)
        class HeldThread:
            def start(self): pass
        return HeldThread()

    def start(self, runner):
        with patch.object(source, 'admitted_thread', side_effect=self.thread):
            return self.store.start(self.payload, runner=runner, requested_by='reviewer')

    def fill(self, size):
        # Pre-fix native format allowed an arbitrary summary. This synthetic
        # retained record represents inventory pressure, not a provider call.
        self.store._write_payload_unlocked({'jobs': [dict(run_id='retained',
            request_key='retained-key', status='success', summary={'trace':'x'*size})]})

    def test_worst_case_full_native_contract_fits_reserve(self):
        for char in ('🙂', '\x01', '"'):
            job = {key:char*5000 for key in (
                'run_id','kind','created_at','started_at','finished_at','requested_by',
                'request_key','request_digest','account_id','report_dir','report_json_path',
                'report_markdown_path','status_sync_run_id','status_sync_report_path','error')}
            job.update(status='error', completion_capacity_bytes=source.SUBMIT_JOB_COMPLETION_CAPACITY_BYTES,
                selected_feedback_ids=[char*160]*20, request_payload={'bounded':char*2000},
                submitted_feedback_ids=[char*160]*20,
                summary={key:2**63-1 for key in source.SUBMIT_JOB_COUNTER_FIELDS})
            job.update({key:2**63-1 for key in source.SUBMIT_JOB_COUNTER_FIELDS})
            job['events']=[{key:char*5000 for key in ('timestamp','event','feedback_id','message','status')}]*200
            job['attempts']=[{key:char*5000 for key in ('feedback_id','attempt_status_label','code','reason','run_id','updated_at')}]*20
            job['skipped']=[{key:char*5000 for key in ('feedback_id','code','reason')}]*20
            normalized = source._normalize_submit_job(job)
            self.assertEqual(len(normalized['events']),200)
            self.assertEqual(len(normalized['attempts']),20)
            self.assertEqual(len(normalized['skipped']),20)
            self.assertLess(source._submit_job_storage_bytes(normalized), source.SUBMIT_JOB_COMPLETION_CAPACITY_BYTES)
            # All permitted source bytes, rather than just this fixture's 2k.
            self.assertLess(source._submit_job_storage_bytes(normalized)
                + source.SUBMIT_JOB_SOURCE_MAX_BYTES, source.SUBMIT_JOB_COMPLETION_CAPACITY_BYTES)

    def test_capacity_failure_before_job_thread_or_provider_no_key_eviction(self):
        self.fill(source.SUBMIT_JOB_STORE_MAX_BYTES - source.SUBMIT_JOB_COMPLETION_CAPACITY_BYTES + 10000)
        before=self.store.path.read_bytes(); calls=[]
        with self.assertRaisesRegex(ValueError,'capacity_exceeded'):
            self.start(lambda payload: calls.append(payload))
        self.assertEqual(self.threads,[]); self.assertEqual(calls,[])
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertIsNone(self.store.get_request(self.payload,'reviewer'))

    def test_actual_worker_postwrite_terminal_max_growth_survives_and_exact_retry(self):
        self.fill(source.SUBMIT_JOB_STORE_MAX_BYTES - source.SUBMIT_JOB_COMPLETION_CAPACITY_BYTES - 10000)
        calls=[]
        def provider(payload):
            calls.append(payload)
            persisted=self.store._find_job_unlocked(payload['run_id'])
            self.assertEqual(persisted['completion_capacity_bytes'], source.SUBMIT_JOB_COMPLETION_CAPACITY_BYTES)
            self.assertFalse(persisted['completion_capacity_released'])
            rows=[dict(feedback_id=target, submitted=i<5,
                skip_reason='🙂'*600, submit_result='🙂'*600) for i,target in enumerate(payload['feedback_ids'])]
            attempts=[dict(feedback_id=target,run_id=payload['run_id'],
                attempt_status='submitted' if i<5 else 'skipped',
                code='row_submit_confirmed_success' if i<5 else 'row_skipped',
                reason='🙂'*800,attempt_status_label='🙂'*80,updated_at='🙂'*80)
                for i,target in enumerate(payload['feedback_ids'])]
            return dict(finished_at='2026-07-03T12:00:00Z',rows=rows,attempts=attempts,
                events=[dict(timestamp='🙂'*80,event='🙂'*80,feedback_id=payload['feedback_ids'][0],
                    message='🙂'*600,status='🙂'*80)]*200,
                aggregate=dict(selected_count=20,tested_count=20,submitted_count=5,skipped_count=15,error_count=0),
                artifact_paths={'json':'🙂'*600,'markdown':'🙂'*600},
                status_sync={'error':'readback pending','run_id':'🙂'*160,'report_json_path':'🙂'*600})
        accepted=self.start(provider)
        thread=self.threads[0]; thread['target'](*thread['args'])
        result=self.store.get(accepted['run_id'],actor='reviewer',account_id='fixture-account')
        self.assertEqual(result['status'],'success'); self.assertTrue(result['status_sync_pending'])
        self.assertEqual(len(result['attempts']),20); self.assertEqual(len(result['events']),200)
        self.assertEqual(result['submitted_feedback_ids'],self.payload['feedback_ids'][:5])
        self.assertEqual(len(self.store._read_payload_unlocked()['jobs']),2)
        self.assertLessEqual(self.store.path.stat().st_size,source.SUBMIT_JOB_STORE_MAX_BYTES)
        self.assertTrue(self.store._find_job_unlocked(accepted['run_id'])['completion_capacity_released'])
        # Exact recovery is read-only, never a new native provider/thread.
        before=self.store.path.read_bytes()
        self.assertEqual(self.start(provider)['run_id'],accepted['run_id'])
        self.store._run(accepted['run_id'],self.payload,provider)
        self.assertEqual(len(calls),1); self.assertEqual(len(self.threads),1)
        self.assertEqual(self.store.path.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'cannot_reopen'):
            self.store.patch(accepted['run_id'],{'status':'running'})

    def test_old_active_without_reservation_stops_with_retained_identity_before_provider(self):
        checked=source._validate_submit_selected_payload(self.payload)
        self.store._write_payload_unlocked({'jobs':[dict(run_id='old-job', status='running',
            requested_by='reviewer',request_key=self.payload['request_key'],account_id=self.payload['account_id'],
            selected_feedback_ids=self.payload['feedback_ids'],request_payload=source._submit_request_operands(checked),
            request_digest=source._submit_request_digest(checked,'reviewer'))]})
        calls=[]; self.store._run('old-job',checked,lambda payload:calls.append(payload))
        saved=self.store.get('old-job'); self.assertEqual(saved['status'],'error')
        self.assertIn('capacity_not_reserved',saved['error']); self.assertEqual(calls,[])
        self.assertEqual(self.start(lambda payload:calls.append(payload))['run_id'],'old-job')
        self.assertEqual(self.threads,[]); self.assertEqual(calls,[])

    def test_operand_bounds_are_checked_before_native_identity_or_thread(self):
        for extra in ({'stars':[1]*200},{'timeout_ms':{'nested':'x'}},{'max_api_rows':2**200},
            {'date_from':'x'*500},{'retry_errors':['x']}):
            with self.subTest(extra=extra), patch.object(source,'admitted_thread',side_effect=self.thread):
                with self.assertRaises(ValueError):
                    self.store.start(dict(self.payload,**extra),runner=lambda payload:self.fail('provider ran'))
        self.assertEqual(self.threads,[]); self.assertFalse(self.store.path.exists())

    def test_count_capacity_before_admission_no_retained_keys_removed(self):
        jobs=[dict(run_id='retained-'+str(i),request_key='key-'+str(i),status='success') for i in range(5000)]
        self.store._write_payload_unlocked({'jobs':jobs})
        before=self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'capacity_exceeded'):
            self.start(lambda payload:self.fail('provider ran'))
        self.assertEqual(self.threads,[]); self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(len(self.store._read_payload_unlocked()['jobs']),5000)

    def test_intermediate_error_keeps_reserve_and_restart_does_not_replay(self):
        accepted=self.start(lambda payload:self.fail('provider ran'))
        self.store.patch(accepted['run_id'],{'status':'error','error':'write unknown'})
        self.assertFalse(self.store._find_job_unlocked(accepted['run_id'])['completion_capacity_released'])
        other=self.store.start(dict(self.payload,request_key='other'),runner=lambda payload:self.fail('provider ran'))
        self.assertTrue(other['not_accepted']); self.assertFalse(other['run_id'])
        from datetime import datetime, timezone, timedelta
        restarted=source.JsonFileFeedbacksComplaintsSubmitJobStore(self.runtime,journal=self.store.journal,
            now_factory=lambda:datetime.now(timezone.utc)+timedelta(hours=1))
        saved=restarted._find_job_unlocked(accepted['run_id'])
        self.assertTrue(saved['completion_capacity_released']); self.assertEqual(saved['status'],'error')
        self.assertEqual(saved['request_key'],self.payload['request_key'])
        self.assertEqual(restarted.get_request(self.payload,'reviewer')['run_id'],accepted['run_id'])


if __name__=='__main__': unittest.main()
