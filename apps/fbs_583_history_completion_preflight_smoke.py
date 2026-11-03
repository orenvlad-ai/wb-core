"""Offline-only semantic checks: no admission, runtime bootstrap or execution."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application import fbs_583_history_completion_preflight as preflight
from apps.fbs_583_history_completion_preflight import main


class CompletionPreflight(unittest.TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.case=self.root/'case.json'
        self.ready={key:key+'-value' for key in ('bundle_version','as_of_date','snapshot_id','plan_version',
            'refreshed_at','publication_operation_id','publication_attempt_id','ready_fingerprint')}
        self.versions=dict(fbs_book='current-book',functional_version='functional',warehouse_run='warehouse',weekly_cost_fingerprint='finance')
        self.cycle=dict(cycle_id=preflight.CYCLE,owner_pid=123,job_id='old-job',business_date='2026-11-03',
            history_config_fingerprint='old-history-config',final_versions=self.versions,
            stages=[dict(stage=name,status='complete',finished_at='2026-11-03T08:00:00Z',versions={'proof':name}) for name in preflight.STAGES])
        self.cycle['stages'][1]['status']='degraded'
        self.cycle['stages'][7]['versions']=self.ready
        self.cycle['stages'][8].update(status='running',finished_at=None,versions={})
        self.finance=dict(status='applied',fingerprint='finance',checked_week_count=8,recalculated_week_count=1,
            weeks={'details_omitted':True,'item_count':1},non_target_preserved=True,post_verify_stale_week_count=0,
            accounting_version_unchanged=True,accounting_version_before='current-book',accounting_version='current-book',
            source_advanced_after_apply=False)
        self.metadata=dict(capture={'observed_at':'2026-11-03T10:00:00Z'},
            maintenance=dict(phase='held',barrier_phase='held',window_id='held-window',barrier_window_id='held-window',
                baseline_fingerprint='baseline',quiet=None,hold_confirmed=True),
            publications=dict(parent=dict(operation_id=preflight.OPERATION,attempt_id='1',
                kind='fbs_temporary_allocation_effect_intent_v1',state='prepared',recipe_digest=preflight.RECIPE),
                child=dict(operation_id=preflight.OPERATION+':publication',attempt_id='1',
                kind='fbs_temporary_allocation_publication_v1',state='complete',book_version=preflight.HISTORICAL_BOOK)),
            old_owner=dict(pid=123,proc_exists=False),active=dict(book_version='current-book',functional_version='functional'),
            ready={**self.ready,'publication_state':'complete','actual_ready_digest':'ready-digest','after_digest':'ready-digest'},
            history_current={'current':'old-edition'},history_pending_exists=False,
            warehouse=dict(run_id='warehouse',status='success',functional_version_id='functional',phases={'phase':'success'}),
            finance_provenance=dict(warehouse_run_id='warehouse',phase_key='dependent_replay_economics'),
            formula=dict(formula_epoch='installed-old-epoch',runtime_contract_sha256='installed-contract'))
        self.refs={}
        for name,data in [('parent_inputs',{'raw':'unchanged-parent'}),('child_inputs',{'raw':'unchanged-child'})]:
            self.write(name,data)
            self.metadata['publications']['parent' if name=='parent_inputs' else 'child'].update(
                inputs_sha256=self.refs[name]['sha256'],inputs_bytes=(self.root/(name+'.json')).stat().st_size)
        self.save()

    def write(self,name,value):
        path=self.root/(name+'.json');path.write_text(json.dumps(value,sort_keys=True))
        self.refs[name]=dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def save(self):
        self.write('cycle_receipt',self.cycle)
        self.write('finance_receipt',self.finance)
        self.metadata['finance_provenance']['receipt_sha256']=self.refs['finance_receipt']['sha256']
        self.write('captured_evidence',{'warehouse_run':{'run_id':'warehouse','status':'success','functional_version_id':'functional',
            'result_json':json.dumps({'wb_finance_cost_recalculation':self.finance})}})
        self.write('runtime_contract',{'formula_epoch':'installed-old-epoch'})
        self.metadata['formula']['runtime_contract_sha256']=self.refs['runtime_contract']['sha256']
        self.write('current_metadata',self.metadata)
        self.case.write_text(json.dumps({'schema_version':preflight.SCHEMA,'artifacts':self.refs}))

    def inspect(self,now='2026-11-03T10:01:00Z'):
        return preflight.inspect_case(self.case,now=now)

    def test_saved_success_is_not_execution_or_fresh_finance_authority_and_does_not_write(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        with (patch('sqlite3.connect',side_effect=AssertionError('no SQLite/native runtime in offline preflight')),
              patch('socket.create_connection',side_effect=AssertionError('no external source'))):
            report=self.inspect()
        self.assertEqual(report['conflicts'],[])
        self.assertFalse(report['execution_supported']);self.assertFalse(report['ready'])
        self.assertEqual(report['finance']['live_validation'],'not_performed')
        self.assertEqual(report['finance']['coverage'],'summarized_target_scope')
        self.assertEqual(report['finance']['checked_week_count'],8)
        self.assertEqual(report['finance']['recalculated_week_count'],1)
        self.assertTrue(report['finance']['receipt_provenance_verified'])
        self.assertIn('read_only_native_finance_validation_of_all_current_candidate_weeks',report['missing_native_interfaces'])
        held=next(c for c in report['checks'] if c['name']=='held_maintenance_observation')
        self.assertEqual(held['status'],'unknown');self.assertIn('quiet',held['missing'])
        self.assertTrue(report['formula_transition']['requires_explicit_formula_transition'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})

    def test_missing_or_unfinished_predecessor_is_not_reusable(self):
        self.cycle['stages'][4]['status']='running';self.save()
        self.assertIn('original_cycle_finished_prefix',self.inspect()['conflicts'])
        self.cycle['stages'].pop(0);self.save()
        self.assertIn('original_cycle_finished_prefix',self.inspect()['conflicts'])

    def test_book_ready_owner_and_native_receipt_identity_drift_remain_explicit(self):
        self.metadata['active']['book_version']='changed'
        self.metadata['ready']['actual_ready_digest']='changed'
        self.metadata['old_owner']['proc_exists']=True
        self.metadata['publications']['parent']['recipe_digest']='another-recipe'
        self.save();conflicts=self.inspect()['conflicts']
        for name in ('active_book_and_functional_predecessor','ready_saved_publication_readback','old_owner_observation','original_parent_identity'):
            self.assertIn(name,conflicts)
        self.metadata['formula'].update(runtime_contract_sha256='0'*64,formula_epoch='contradictory-epoch')
        self.write('current_metadata',self.metadata)
        self.case.write_text(json.dumps({'schema_version':preflight.SCHEMA,'artifacts':self.refs}))
        self.assertIn('installed_formula_contract_observation',self.inspect()['conflicts'])

    def test_raw_tamper_and_nonboolean_finance_flags_cannot_pass(self):
        path=self.root/'child_inputs.json';path.write_bytes(path.read_bytes().replace(b'child',b'CHILD'))
        with self.assertRaisesRegex(preflight.PreflightEvidenceError,'artifact_sha256_mismatch'):self.inspect()
        output=StringIO()
        with redirect_stdout(output):
            code=main(['--case-manifest',str(self.case),'--now','2026-11-03T10:01:00Z'])
        self.assertEqual(code,2);self.assertFalse(json.loads(output.getvalue())['execution_supported'])
        path.write_bytes(path.read_bytes().replace(b'CHILD',b'child'))
        self.finance['non_target_preserved']=1;self.save()
        self.assertIn('saved_finance_consistency',self.inspect()['conflicts'])

    def test_day_advance_and_unbound_finance_are_not_silently_accepted(self):
        self.assertIn('cycle_business_day',self.inspect('2026-11-04T10:01:00Z')['conflicts'])
        other=deepcopy(self.finance);other['fingerprint']='another'
        self.write('captured_evidence',{'warehouse_run':{'result_json':json.dumps({'wb_finance_cost_recalculation':other})}})
        self.case.write_text(json.dumps({'schema_version':preflight.SCHEMA,'artifacts':self.refs}))
        self.assertIn('finance_matches_captured_warehouse_result',self.inspect()['conflicts'])
        self.write('captured_evidence',{'warehouse_run':{'run_id':'another-run','status':'success','functional_version_id':'functional',
            'result_json':json.dumps({'wb_finance_cost_recalculation':self.finance})}})
        self.case.write_text(json.dumps({'schema_version':preflight.SCHEMA,'artifacts':self.refs}))
        report=self.inspect()
        self.assertIn('finance_captured_warehouse_binding',report['conflicts'])
        self.assertFalse(report['finance']['receipt_provenance_verified'])


if __name__=='__main__':unittest.main()
