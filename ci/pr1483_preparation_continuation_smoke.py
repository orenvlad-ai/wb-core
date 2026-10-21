"""Prove PR1483 continuation refuses drift, replay and unknown preparation."""
from __future__ import annotations
import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci import pr1483_preparation_continuation as c
from ci import post_merge_release_recovery as r
from ci.post_merge_release_recovery_smoke import CommentsClient, preview
from packages.application import business_data_deploy_protection as deploy
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_write_barrier as barrier


def rejects(call):
    try:
        call()
    except (r.RecoveryError, SystemExit):
        return
    raise AssertionError('unexpected admission')


def test_parent():
    receipt = {'schema': r.RECOVERY_SCHEMA, 'state': 'ambiguous', 'operation_id': c.PARENT,
        'preview_fingerprint': c.OLD_FINGERPRINT, 'reason': 'autoanswers-prepare-deploy-failed-1',
        'source': {'merge_sha': c.MERGE}, 'completed_stages': [{'stage': s} for s in
        ['deploy-owner-claim', 'deploy-owner-recovery-start', 'root-storage-readback', 'status', 'auth',
         'auth-preflight', 'root-storage-status', 'systemd-barrier-preflight']]}
    run = dict(name='Post-merge Release Recovery', path='.github/workflows/release-recovery.yml', event='workflow_dispatch',
        run_attempt=1, status='completed', conclusion='failure', head_sha='0d2983cd86db04e2f2ed1ae7cd10595a201e1a89',
        run_started_at='2026-10-21T08:17:00Z')
    phases = [{'state': state, 'phase': phase, 'operation_id': c.PARENT, 'preview_fingerprint': c.OLD_FINGERPRINT}
        for phase in ('01-auth-preflight', '02-root-storage-status', '03-systemd-barrier-preflight') for state in ('before', 'after')]
    phases.append(dict(state='before', phase='04-autoanswers-prepare-deploy', operation_id=c.PARENT, preview_fingerprint=c.OLD_FINGERPRINT))
    claim = dict(state='claimed', source=receipt['source'], preview_fingerprint=c.OLD_FINGERPRINT)
    class Client:
        def get(self, path):
            return {'artifacts': [dict(id=1, name=f'release-recovery-{c.ORIGINAL_RUN}-{c.FAILED_RUN}', expired=False)]} if 'artifacts?' in path else run
        def request(self, *args, **kwargs):
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as z: z.writestr('recovery-receipt.json', json.dumps(receipt))
            return output.getvalue()
    def comments(client, pr, marker):
        return [claim] if marker == r._claim_marker(c.PARENT) else [] if marker == r._receipt_marker(c.PARENT) else phases
    with patch.object(r, '_matching_comments', side_effect=comments):
        assert c.parent_evidence(Client())['run_id'] == c.FAILED_RUN
        for obj, key, value in [(run, 'run_attempt', 2), (run, 'conclusion', 'success'), (run, 'head_sha', 'x'),
                                (receipt, 'reason', 'autoanswers-prepare-deploy-failed-255'), (receipt, 'preview_fingerprint', 'x'),
                                (receipt, 'completed_stages', []), (claim, 'state', 'complete')]:
            old = obj[key]; obj[key] = value; rejects(lambda: c.parent_evidence(Client())); obj[key] = old
        phases.append(dict(phases[-1], state='after'))
        rejects(lambda: c.parent_evidence(Client()))


def test_remote():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); (root/'code.py').write_text('guarded')
        fragment = root/'sync.service'; fragment.write_text('known guarded unit')
        units = {'sync.timer': {'config': 'timer', 'is_active': 'inactive'}, c.SYNC: {'config': 'sync', 'properties': {'Result': 'exit-code'}}}
        state = {'phase': 'held', 'window_id': c.WINDOW, 'baseline_fingerprint': c.BASELINE,
                 'baseline': {'controls': {}, 'units': deepcopy(units)}}
        current = {'quiet': True, 'admission': {'idle': True}, 'controls': {}, 'units': deepcopy(units)}
        owner = {'phase': 'mutation_started', 'operation_id': 'original'}
        bar = {'active': True, 'hold_confirmed': True, 'window_id': c.WINDOW, 'state_fingerprint': 'barrier-proof'}
        registry = {'Id': 'wb-core-registry-http.service', 'ActiveState': 'active', 'SubState': 'running',
                    'Result': 'success', 'MainPID': '123', 'InvocationID': 'original-registry',
                    'ActiveEnterTimestamp': 'Thu 2026-10-15 02:09:50 UTC'}
        sync = {'Id': c.SYNC, 'ActiveState': 'failed', 'Result': 'exit-code', 'MainPID': '0', 'ExecMainStatus': '1',
                'InvocationID': 'failed-sync', 'ExecMainExitTimestamp': 'Wed 2026-10-21 07:43:06 UTC'}
        expected = {'app_dir': str(root), 'runtime_dir': temp, 'env_file': str(root/'env'), 'operation': 'original',
                    'expected_sha': c.MERGE, 'window': c.WINDOW, 'baseline': c.BASELINE, 'main_pid': 123,
                    'started_at': '2026-10-21T08:17:00Z', 'sync': c.SYNC,
                    'code_hashes': {'code.py': r.digest((root/'code.py').read_bytes())}, 'sync_unit_sha256': r.digest(fragment.read_bytes())}
        starts = []; skip = True
        def command(args, **kwargs):
            if args[:2] == ['systemctl', 'show']:
                value = {'FragmentPath': str(fragment), 'DropInPaths': ''} if 'FragmentPath' in args[3] else registry if args[2] == registry['Id'] else sync
                return SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k,v in value.items()))
            if args == ['systemctl', 'start', c.SYNC]:
                starts.append(args); sync.update(ActiveState='inactive', Result='success', ExecMainStatus='0'); return SimpleNamespace(returncode=0)
            if args[0] == 'journalctl':
                return SimpleNamespace(stdout=json.dumps({'__REALTIME_TIMESTAMP': str(int(datetime.now(timezone.utc).timestamp()*1e6)),
                    '_SYSTEMD_UNIT': c.SYNC, 'MESSAGE': '{"status":"skipped_maintenance"}' if skip else 'unknown'}))
            raise AssertionError('unexpected command: ' + repr(args))
        def execute(clear=False):
            output=io.StringIO()
            with contextlib.redirect_stdout(output): exec(c.proof_source(expected, clear=clear), {})
            return json.loads(output.getvalue())
        with patch.object(deploy, 'require_owner', return_value=owner), patch.object(deploy, '_clients', return_value=(None, lambda: current)), \
             patch.object(pause, 'load_state', return_value=state), patch.object(pause, '_unit_fingerprint', side_effect=lambda u:u['config']), \
             patch.object(barrier, 'barrier_status', return_value=bar), patch.object(subprocess, 'run', side_effect=command):
            proof=execute(); assert starts == []
            for obj,key,value in [(owner,'phase','complete'),(state,'window_id','other'),(state,'baseline_fingerprint','other'),
                (bar,'active',False),(bar,'hold_confirmed',False),(current,'quiet',False),(current,'controls',{'drift':True}),
                (registry,'MainPID','124'),(registry,'ActiveEnterTimestamp','Wed 2026-10-21 08:18:00 UTC'),
                (sync,'Result','success'),(sync,'MainPID','100'),(sync,'ExecMainExitTimestamp','Wed 2026-10-21 08:18:00 UTC'),
                (current['units']['sync.timer'],'is_active','active'),(current['units'][c.SYNC],'config','drift'),
                (expected,'sync_unit_sha256','wrong')]:
                old=obj[key]; obj[key]=value; rejects(execute); obj[key]=old; assert starts == []
            expected['reviewed_proof'] = {'wrong': True}; rejects(lambda: execute(True)); assert starts == []
            expected['reviewed_proof'] = proof
            result=execute(True); assert result['status']=='skipped_maintenance' and len(starts)==1
            rejects(lambda: execute(True)); assert len(starts)==1
            sync.update(ActiveState='failed', Result='exit-code', ExecMainStatus='1'); skip=False
            rejects(lambda: execute(True)); assert len(starts)==2  # one attempt in this separate failure scenario


def test_fingerprint_and_replay():
    p=preview(); baseline=r.preview_fingerprint(p)
    p['pr1483_preparation_continuation']={'proof':1}; first=r.preview_fingerprint(p)
    p['pr1483_preparation_continuation']['proof']=2
    assert baseline != first != r.preview_fingerprint(p)
    client=CommentsClient(); r._publish_once(client,1483,r._claim_marker(c.OPERATION),{'state':'claimed','preview_fingerprint':'f'})
    with patch.object(c,'build_preview',side_effect=AssertionError('must not rebuild/replay')), \
         patch.object(r,'apply_recovery',side_effect=AssertionError('must not apply')):
        assert c.run(client,None,'apply','f')['state']=='blocked'
        rejects(lambda:c.run(client,None,'apply','wrong'))


if __name__ == '__main__':
    test_parent(); test_remote(); test_fingerprint_and_replay()
    print('PASS: exact lineage, unentered preparation, guarded skip, drift refusal, fingerprint and replay protection')
