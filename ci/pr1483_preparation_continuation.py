"""One bounded continuation after PR1483's demonstrably unentered preparation.

The old recovery claim is retained. No ambiguous write is replayed: the still
running original registry invocation proves prepare-deploy never entered its
schema/migration block, which requires stopping that invocation first.
"""
from __future__ import annotations

import json
import subprocess
import textwrap

from ci import post_merge_release_recovery as r

FAILED_RUN = 39568090548
ORIGINAL_RUN = 39564470075
MERGE = 'b741b3535fe5e4dfc2831a6784cf1cac6124d046'
PARENT = 'release-recovery-v1-ec1bb2fd35d4d279e400788224fdeefb'
OPERATION = PARENT + '-prepare-continuation-39568090548'
OLD_FINGERPRINT = '01af7850b91966e883696802f1ffba715fd5c17656476add7ba156c9f63b706d'
WINDOW = 'PR1483-saved-history-20261021'
BASELINE = 'sha256:c29167ad3a7d17437771ef1452e40e33dfc221daaa9f27404ebe8692f81eaacd'
SYNC = 'wb-core-autoanswers-readonly-sync.service'
CODE_PATHS = ('apps/wb_autoanswers_activation.py', 'apps/wb_autoanswers_readonly.py',
              'packages/application/business_data_procedure_admission.py',
              'packages/application/business_data_write_barrier.py')


def require(condition, reason):
    if not condition:
        raise r.RecoveryError('pr1483-continuation-' + reason)


def parent_evidence(client):
    run = client.get(f'/actions/runs/{FAILED_RUN}')
    require(tuple(run.get(k) for k in ('name', 'path', 'event', 'run_attempt', 'status', 'conclusion', 'head_sha')) ==
            ('Post-merge Release Recovery', '.github/workflows/release-recovery.yml', 'workflow_dispatch',
             1, 'completed', 'failure', '0d2983cd86db04e2f2ed1ae7cd10595a201e1a89'), 'parent-run')
    items = client.get(f'/actions/runs/{FAILED_RUN}/artifacts?per_page=100').get('artifacts', [])
    artifacts = [a for a in items if a.get('name') == f'release-recovery-{ORIGINAL_RUN}-{FAILED_RUN}' and not a.get('expired')]
    require(len(artifacts) == 1, 'parent-artifact')
    raw = client.request('GET', f"/actions/artifacts/{int(artifacts[0]['id'])}/zip", raw=True)
    receipt = r._json_file(r._zip_files(raw, 'pr1483-parent'), 'recovery-receipt.json', 'pr1483-parent')
    require(receipt.get('schema') == r.RECOVERY_SCHEMA and receipt.get('state') == 'ambiguous'
            and receipt.get('operation_id') == PARENT and receipt.get('preview_fingerprint') == OLD_FINGERPRINT
            and receipt.get('reason') == 'autoanswers-prepare-deploy-failed-1'
            and receipt.get('source', {}).get('merge_sha') == MERGE, 'parent-receipt')
    require([s.get('stage') for s in receipt.get('completed_stages', [])] ==
            ['deploy-owner-claim', 'deploy-owner-recovery-start', 'root-storage-readback', 'status', 'auth',
             'auth-preflight', 'root-storage-status', 'systemd-barrier-preflight'], 'parent-stages')
    claims = r._matching_comments(client, 1483, r._claim_marker(PARENT))
    require(len(claims) == 1 and claims[0].get('state') == 'claimed'
            and claims[0].get('source') == receipt['source']
            and claims[0].get('preview_fingerprint') == OLD_FINGERPRINT, 'parent-claim')
    require(not r._matching_comments(client, 1483, r._receipt_marker(PARENT)), 'parent-completed')
    phases = r._matching_comments(client, 1483, f'<!-- wb-core-release-recovery-phase operation={PARENT} ')
    expected = [(state, phase) for phase in ('01-auth-preflight', '02-root-storage-status', '03-systemd-barrier-preflight')
                for state in ('before', 'after')] + [('before', '04-autoanswers-prepare-deploy')]
    require(sorted((p.get('state'), p.get('phase')) for p in phases) == sorted(expected)
            and all(p.get('operation_id') == PARENT and p.get('preview_fingerprint') == OLD_FINGERPRINT for p in phases), 'parent-phases')
    return {'run_id': FAILED_RUN, 'receipt_sha256': r.digest(r.canonical_bytes(receipt)),
            'source': receipt['source'], 'started_at': run['run_started_at'], 'artifact_id': artifacts[0]['id']}


def proof_source(expected, *, clear=False):
    # Pure reads until the explicit, durable-claim-protected clear branch.
    return 'import json, hashlib, subprocess, sys\nfrom pathlib import Path\nfrom datetime import datetime, timezone\n' + \
        'e = json.loads(' + repr(json.dumps(expected)) + ')\n' + textwrap.dedent('''
        sys.path.insert(0, e['app_dir'])
        from packages.application import business_data_deploy_protection as deploy
        from packages.application import business_data_maintenance_pause as pause
        from packages.application.business_data_write_barrier import barrier_status
        runtime = Path(e['runtime_dir'])
        owner = deploy.require_owner(runtime, e['operation'], e['expected_sha'])
        state = pause.load_state(runtime)
        barrier = barrier_status(runtime)
        if not (owner['phase'] == 'mutation_started'): raise SystemExit(40)
        if not (state['phase'] == 'held' and state['window_id'] == e['window']): raise SystemExit(40)
        if not (state['baseline_fingerprint'] == e['baseline']): raise SystemExit(40)
        if not (barrier['active'] and barrier['hold_confirmed'] and barrier['window_id'] == e['window']): raise SystemExit(40)
        _systemd, reader = deploy._clients(runtime, Path(e['env_file']))
        current = reader()
        if not (current['quiet'] and current['admission']['idle']): raise SystemExit(40)
        if not (current['controls'] == state['baseline']['controls']): raise SystemExit(40)
        if not (current['units'].keys() == state['baseline']['units'].keys()): raise SystemExit(40)
        for unit, value in current['units'].items():
            if not (pause._unit_fingerprint(value) == pause._unit_fingerprint(state['baseline']['units'][unit])): raise SystemExit(40)
            if unit.endswith('.timer'):
                if not (value['is_active'] == 'inactive'): raise SystemExit(40)
        for name, digest in e['code_hashes'].items():
            if not (hashlib.sha256((Path(e['app_dir']) / name).read_bytes()).hexdigest() == digest): raise SystemExit(40)
        def show(unit):
            raw = subprocess.run(['systemctl', 'show', unit, '--property=Id,ActiveState,SubState,MainPID,Result,ExecMainStatus,InvocationID,ActiveEnterTimestamp,ExecMainExitTimestamp', '--no-pager'],
                capture_output=True, text=True, check=True, timeout=15).stdout
            return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        def timestamp(value):
            return datetime.strptime(value, '%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc).timestamp()
        started = datetime.fromisoformat(e['started_at'].replace('Z', '+00:00')).timestamp()
        registry = show('wb-core-registry-http.service')
        if not (registry['ActiveState'] == 'active' and registry['SubState'] == 'running'): raise SystemExit(40)
        if not (registry['Result'] == 'success' and int(registry['MainPID']) == e['main_pid']): raise SystemExit(40)
        if not (registry['InvocationID'] and timestamp(registry['ActiveEnterTimestamp']) < started): raise SystemExit(40)
        # An unchanged active invocation predating the failed recovery cannot
        # have crossed _deployment_quiesce's mandatory stop-before-yield.
        sync = show(e['sync'])
        if not (sync['ActiveState'] == 'failed' and sync['Result'] == 'exit-code'): raise SystemExit(40)
        if not (sync['MainPID'] == '0' and sync['ExecMainStatus'] == '1'): raise SystemExit(40)
        if not (sync['InvocationID'] and timestamp(sync['ExecMainExitTimestamp']) < started): raise SystemExit(40)
        if not (state['baseline']['units'][e['sync']]['properties']['Result'] == 'exit-code'): raise SystemExit(40)
        unit = subprocess.run(['systemctl', 'show', e['sync'], '--property=FragmentPath,DropInPaths', '--no-pager'],
            capture_output=True, text=True, check=True, timeout=15).stdout
        properties = dict(line.split('=', 1) for line in unit.splitlines() if '=' in line)
        if not (properties['DropInPaths'] == ''): raise SystemExit(40)
        fragment = Path(properties['FragmentPath']).read_bytes()
        if not (hashlib.sha256(fragment).hexdigest() == e['sync_unit_sha256']): raise SystemExit(40)
        proof = {'owner': owner, 'registry': registry, 'sync': sync,
                 'code_hashes': e['code_hashes'], 'baseline': state['baseline_fingerprint'],
                 'barrier_fingerprint': barrier['state_fingerprint']}
        ''') + (textwrap.dedent('''
        if not (proof == e['reviewed_proof']): raise SystemExit(40)
        # Exactly one ordinary invocation, blocked before business code by
        # guard_cli and the held barrier. Never reset-failed or bypass admission.
        before_us = int(datetime.now().timestamp() * 1000000)
        subprocess.run(['systemctl', 'start', e['sync']], check=True, timeout=30)
        after = show(e['sync'])
        if not (after['ActiveState'] == 'inactive' and after['MainPID'] == '0'): raise SystemExit(40)
        if not (after['Result'] == 'success' and after['ExecMainStatus'] == '0'): raise SystemExit(40)
        journal = subprocess.run(['journalctl', '-u', e['sync'], '--since', '@' + str(before_us // 1000000), '-o', 'json', '--no-pager'],
            capture_output=True, text=True, check=True, timeout=15).stdout
        messages = [json.loads(line) for line in journal.splitlines()]
        skipped = [m for m in messages if int(m['__REALTIME_TIMESTAMP']) >= before_us
                   and m.get('_SYSTEMD_UNIT') == e['sync'] and '"skipped_maintenance"' in m.get('MESSAGE', '')]
        if not (len(skipped) == 1): raise SystemExit(40)
        if not (show('wb-core-registry-http.service') == registry): raise SystemExit(40)
        if not (deploy.require_owner(runtime, e['operation'], e['expected_sha']) == owner): raise SystemExit(40)
        if not (barrier_status(runtime) == barrier): raise SystemExit(40)
        print(json.dumps({'status': 'skipped_maintenance', 'service': e['sync'], 'after': after}))
        ''') if clear else "print(json.dumps(proof, sort_keys=True))\n")


def collect_proof(target, preview, parent, *, clear=False):
    identity = r._deploy_protection_identity(target, preview['source'])
    hashes = {p: r.digest(subprocess.run(['git', 'show', MERGE + ':' + p], cwd=r.ROOT,
                                       check=True, capture_output=True).stdout) for p in CODE_PATHS}
    # Find the source-owned exact unit, refusing ambiguity.
    paths = r._git(['ls-tree', '-r', '--name-only', MERGE]).stdout.splitlines()
    units = [p for p in paths if p.endswith('/wb-core-autoanswers-readonly-sync.service')]
    require(len(units) == 1, 'unit-source')
    unit_hash = r.digest(subprocess.run(['git', 'show', MERGE + ':' + units[0]], cwd=r.ROOT, check=True, capture_output=True).stdout)
    expected = {**identity, 'window': WINDOW, 'baseline': BASELINE, 'main_pid': preview['prestate']['main_pid'],
                'started_at': parent['started_at'], 'sync': SYNC, 'code_hashes': hashes, 'sync_unit_sha256': unit_hash}
    if clear:
        expected['reviewed_proof'] = preview['pr1483_preparation_continuation']['unentered_preparation']
    return r._run_remote_json(target, proof_source(expected, clear=clear))


def build_preview(client, target):
    parent = parent_evidence(client)
    preview = r.build_preview(client, ORIGINAL_RUN, target)
    require(preview['source'] == parent['source'] and preview['source']['merge_sha'] == MERGE
            and preview['recovery_case'] == 'normal-worker-health-precheck-activation-tail', 'source')
    proof = collect_proof(target, preview, parent)
    preview['operation_id'] = OPERATION
    preview['pr1483_preparation_continuation'] = {'parent': parent, 'unentered_preparation': proof}
    preview['stages'].insert(0, 'stopped-sync-skipped-maintenance-once')
    preview['preview_fingerprint'] = r.preview_fingerprint(preview)
    return preview


def clear_stopped_sync_health(client, target, preview):
    require(preview['operation_id'] == OPERATION and preview['source']['merge_sha'] == MERGE, 'clear-source')
    parent = parent_evidence(client)
    require(parent == preview['pr1483_preparation_continuation']['parent'], 'parent-drift')
    result = collect_proof(target, preview, parent, clear=True)
    require(result.get('status') == 'skipped_maintenance', 'skip-unproven')
    return {'stage': 'stopped-sync-skipped-maintenance-once', 'stdout_sha256': r.digest(r.canonical_bytes(result))}


def run(client, target, command, fingerprint):
    receipts = r._matching_comments(client, 1483, r._receipt_marker(OPERATION))
    claims = r._matching_comments(client, 1483, r._claim_marker(OPERATION))
    if receipts or claims:
        require(len(claims) == 1 and claims[0].get('preview_fingerprint') == fingerprint, 'existing-claim')
        if not receipts:
            return {'schema': r.RECOVERY_SCHEMA, 'state': 'blocked', 'operation_id': OPERATION,
                    'reason': 'existing-continuation-claim-readback-only'}
        require(len(receipts) == 1 and receipts[0].get('state') == 'complete'
                and receipts[0].get('preview_fingerprint') == fingerprint, 'existing-receipt')
        final = r.collect_prestate(target, MERGE, require_incomplete=False,
                                  case=r.RecoveryCase('normal-worker-health-precheck-activation-tail'))
        return {**receipts[0], 'fresh_final_readback': final}
    preview = build_preview(client, target)
    if command == 'preview':
        return preview
    require(command == 'apply' and preview['preview_fingerprint'] == fingerprint, 'fingerprint')
    return r.apply_recovery(client, preview, fingerprint, target)
