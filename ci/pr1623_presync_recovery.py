"""One exact continuation whose original deploy stopped before owner start/sync.

The canonical adapter, from the admitted immutable merge, owns every deploy
stage. A durable recovery claim permits one invocation; uncertainty is readback
only. This is deliberately not a generic deploy retry facility.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap

from ci import post_merge_release_recovery as r

RUN = 45470334066
PREVIOUS = '336345e6c9b08f6c2741523aa066f93c485a2f6e'
PROFILE = dict(pull_request=1623, gate_run_id=45468017296,
    base_sha='3ed6024fb661f8cc295529673eb4cd84296761de',
    head_sha='2e9a925433e8d8ffd249e3e55dd122c0a74d68f8',
    merge_sha='48aba616911767c1c9410d013f59cfbe258ce4c8',
    operation_id='release-v3-3d39b2cc0010ab71718b7e1fc93e214f')
WINDOW = 'wbc0201-inventory-history-repair-20261124'
BASELINE = 'sha256:ccf821568b4f2a529cbe6e7df5343178da7fb5c9f659bba44268de4927b775d0'
OWNER = 'sha256:8a0216040d13722ee4a5984a85bab2d451d7577fb1f4db653a8b9ec5c7a7c587'
TARGET_RELATIVE = 'artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json'
CODE_PATHS = (*r.RUNTIME_GUARD_PATHS,
    'apps/root_storage_policy.py',
    'packages/application/business_data_deploy_protection.py',
    'packages/application/business_data_maintenance_pause.py',
    'packages/application/business_data_held_formula.py',
    'packages/application/business_data_formula_resume.py',
    'packages/application/business_data_write_barrier.py')


def require(condition, reason):
    if not condition:
        raise r.RecoveryError('pr1623-presync-' + reason)


def prove_failure(raw, gate, job):
    text = raw.decode('utf-8', errors='replace')
    required = ('line 1208, in deploy_current_checkout',
        'run_stage("root-storage-preflight", root_storage_commands["preflight"],',
        'allow_transport_reconciliation=False)',
        '"stage": "root-storage-preflight", "ok": false',
        '"available_bytes": 26385899520', '"required_reserve_bytes": 26843545600',
        '"code": "storage_reserve_breach"', '"role": "root"',
        '"phase": "prepared"', OWNER, BASELINE, PROFILE['operation_id'])
    failures = re.findall(r'(?:subprocess\.)?CalledProcessError:.*?returned non-zero exit status (\d+)', text, re.DOTALL)
    require(gate == PROFILE['gate_run_id'] and all(s in text for s in required)
        and r._storage_tail_gate_argument_matches(text, gate,
            runner_path='ci/github_release_runner.py', allow_expanded=False)
        and failures == ['3'] and 'exit status 255' not in text
        and 'status_artifact_readback"]' not in text
        and '"phase": "mutation_started"' not in text, 'failure-not-exact')
    return dict(job_name=job, job_log_sha256=r.digest(raw),
        stage='root-storage-preflight-before-owner-start', exit_status=3)


def evidence(client):
    result = r.collect_evidence(client, RUN)
    require(all(result['original_receipt'].get(k) == v for k, v in PROFILE.items()), 'source')
    require(result['gate_plan']['plan_sha256'] == '5b822bd7be1fa9b30384e72fb16278cdad4f19d03826c43776d96e57181019e7', 'plan')
    return result


def source_proof():
    # The installed guard and canonical preflight are byte-identical to the
    # candidate. Prove source ordering as well as the immutable traceback.
    require(not r._git(['diff', '--name-only', PREVIOUS, PROFILE['merge_sha'], '--', *CODE_PATHS]).stdout.strip(), 'guard-source-drift')
    adapter = r._git(['show', PROFILE['merge_sha'] + ':' + CODE_PATHS[0]]).stdout
    anchors = ['run_stage("auth-preflight", auth_env_preflight_command)',
        'run_stage("root-storage-preflight", root_storage_commands["preflight"],',
        "for owner_action in ('claim', 'start'):", 'run_stage("mkdir", mkdir_command)',
        'run_stage("sync", rsync_plan)']
    positions = [adapter.index(s) for s in anchors]
    require(positions == sorted(positions) and all(adapter.count(s) == 1 for s in anchors), 'stage-order')
    return {p: r.digest(subprocess.run(['git', 'show', PREVIOUS + ':' + p], cwd=r.ROOT,
        capture_output=True, check=True).stdout) for p in CODE_PATHS}


def owner_script(expected):
    return 'import json, hashlib, sys\nfrom pathlib import Path\n' + 'e = ' + repr(expected) + '\n' + textwrap.dedent('''
        sys.path.insert(0, e['app_dir'])
        from packages.application import business_data_deploy_protection as deploy
        from packages.application import business_data_maintenance_pause as pause
        from packages.application import business_data_held_formula as formula
        runtime = Path(e['runtime_dir'])
        owner = deploy.load(runtime)
        if not owner or owner['operation_id'] != e['operation']: raise SystemExit(40)
        if owner['phase'] not in e['phases'] or owner['expected_sha'] != e['expected_sha']: raise SystemExit(40)
        state = deploy._binding(runtime, owner)
        if state['window_id'] != e['window'] or state['baseline_fingerprint'] != e['baseline']: raise SystemExit(40)
        if e['owner_fingerprint'] and owner['fingerprint'] != e['owner_fingerprint']: raise SystemExit(40)
        systemd, reader, activity = deploy._clients(runtime, Path(e['env_file']), include_activity_reader=True)
        current = reader()
        if not current['quiet'] or not current['admission']['idle']: raise SystemExit(40)
        # During the native final finish, the complete markers and same active
        # owner authorize only finish. The formula proof requires a completed
        # owner and is mandatory in the subsequent terminal readback.
        if owner['phase'] != 'mutation_started':
            formula.unchanged(runtime, state, current, app_dir=Path(e['app_dir']),
                systemd=systemd, activity_reader=activity)
        for name, digest in e['code_hashes'].items():
            if hashlib.sha256((Path(e['app_dir']) / name).read_bytes()).hexdigest() != digest: raise SystemExit(40)
        print(json.dumps({'owner': owner, 'window': state['window_id'],
            'baseline': state['baseline_fingerprint'], 'quiet': True, 'code_hashes': e['code_hashes']}, sort_keys=True))
        ''')


def collect_owner(target, hashes, *, complete=False, finishing=False):
    expected = dict(app_dir=target.target_dir,
        runtime_dir=target.runtime_env['REGISTRY_UPLOAD_RUNTIME_DIR'], env_file=target.environment_file,
        operation=PROFILE['operation_id'], expected_sha=PROFILE['merge_sha'] if complete or finishing else '',
        phases=['mutation_started', 'complete'] if finishing else ['complete' if complete else 'prepared'],
        window=WINDOW, baseline=BASELINE,
        owner_fingerprint='' if complete or finishing else OWNER, code_hashes=hashes)
    return r._run_remote_json(target, owner_script(expected))


def storage_preflight(target):
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    command = hosted._build_root_storage_policy_commands(target)['preflight']
    require(bool(command), 'missing-storage-guard')
    r._must_succeed('root-storage-preflight', command)
    return {'stage': 'root-storage-preflight', 'ok': True}


def fingerprint(value):
    return r.digest(r.canonical_bytes({k: v for k, v in value.items() if k != 'preview_fingerprint'}))


def build_preview(client, target):
    proof = evidence(client)
    original = proof['original_receipt']
    hashes = source_proof()
    require(target.target_id == 'wb_core_eu_hosted_runtime_active', 'target')
    result = dict(schema=r.RECOVERY_SCHEMA, state='reviewable', mode='preview', release_run_id=RUN,
        recovery_case='pr1623-proven-unentered-presync', operation_id=r.recovery_operation_id(RUN, original),
        source=original, failure=proof['failure'], runner=r.prove_repo_only_descendant(client, original),
        prestate=r.collect_prestate(target, PREVIOUS, require_incomplete=False),
        prepared_owner=collect_owner(target, hashes), storage=storage_preflight(target),
        stages=['canonical-exact-merge-deploy-once', 'canonical-runtime-readback',
            'held-owner-finish', 'complete-runtime-finance-and-owner-readback'], forbidden_stages=['merge', 'deploy-replay'])
    result['preview_fingerprint'] = fingerprint(result)
    return result


def canonical_deploy():
    # Keep the trusted repo-only runner checked out. The deploy adapter computes
    # its identity from HEAD, so give it a separate clean exact-merge clone.
    # A linked worktree has a .git FILE, which the original adapter's '.git/'
    # directory-only rsync exclusion would copy into production. A local shared
    # clone keeps the original adapter untouched and excludes its .git directory.
    with tempfile.TemporaryDirectory(prefix='wb-core-pr1623-exact-') as directory:
        checkout = Path(directory) / 'source'
        r._git(['clone', '--shared', '--no-checkout', str(r.ROOT), str(checkout)])
        r._git(['-C', str(checkout), 'checkout', '--detach', PROFILE['merge_sha']])
        require((checkout / '.git').is_dir(), 'checkout-git-must-be-directory')
        require(r._git(['-C', str(checkout), 'rev-parse', 'HEAD']).stdout.strip() == PROFILE['merge_sha'], 'checkout-sha')
        env = os.environ.copy()
        env.update(WB_CORE_RELEASE_PR=str(PROFILE['pull_request']),
            WB_CORE_RELEASE_HEAD=PROFILE['head_sha'],
            WB_CORE_RELEASE_OPERATION_ID=PROFILE['operation_id'],
            WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH='true',
            WB_CORE_HOSTED_RUNTIME_TARGET_FILE=str(checkout / TARGET_RELATIVE))
        subprocess.run([sys.executable, 'apps/registry_upload_http_entrypoint_hosted_runtime.py', 'deploy'],
            cwd=checkout, env=env, check=True)


def run(client, target, command, expected_fingerprint):
    require(command in {'preview', 'apply'}, 'command')
    proof = evidence(client)
    original = proof['original_receipt']
    runner = r.prove_repo_only_descendant(client, original)
    operation = r.recovery_operation_id(RUN, original)
    claims = r._matching_comments(client, 1623, r._claim_marker(operation))
    receipts = r._matching_comments(client, 1623, r._receipt_marker(operation))
    if claims or receipts:
        require(len(claims) == 1 and claims[0].get('source') == original
            and claims[0].get('operation_id') == operation and claims[0].get('state') == 'claimed', 'existing-claim')
        if command == 'apply':
            require(claims[0].get('preview_fingerprint') == expected_fingerprint, 'existing-fingerprint')
        if receipts:
            require(len(receipts) == 1 and receipts[0].get('state') == 'complete'
                and receipts[0].get('source') == original
                and receipts[0].get('preview_fingerprint') == claims[0]['preview_fingerprint'], 'existing-receipt')
        try:
            final = r.collect_prestate(target, PROFILE['merge_sha'], require_incomplete=False)
            require(final['finance_pilot'] == claims[0].get('finance_acceptance'), 'finance-drift')
            hashes = source_proof()
            collect_owner(target, hashes, finishing=True)
        except Exception:
            return dict(schema=r.RECOVERY_SCHEMA, state='ambiguous', operation_id=operation,
                reason='existing-claim-readback-only', source=original)
        if command == 'preview':
            return dict(schema=r.RECOVERY_SCHEMA, state='reviewable', operation_id=operation,
                preview_fingerprint=claims[0]['preview_fingerprint'], source=original,
                stages=['readback-only-owner-finish-and-terminal-receipt'], final_readback=final)
        require(r.prove_repo_only_descendant(client, original) == runner, 'final-main-drift')
        r._finish_deploy_protection(target, original)
        owner = collect_owner(target, hashes, complete=True)
        require(r.prove_repo_only_descendant(client, original) == runner, 'final-main-drift')
        if receipts:
            return {**receipts[0], 'fresh_final_readback': final, 'fresh_owner_readback': owner, 'runner': runner}
        result = dict(schema=r.RECOVERY_SCHEMA, state='complete', operation_id=operation,
            source=original, preview_fingerprint=claims[0]['preview_fingerprint'], runner=runner,
            final_readback=final, final_owner=owner)
        r._publish_once(client, 1623, r._receipt_marker(operation), result)
        return result
    preview = build_preview(client, target)
    if command == 'preview':
        return preview
    require(command == 'apply' and preview['preview_fingerprint'] == expected_fingerprint, 'fingerprint')
    claim = dict(schema=r.RECOVERY_SCHEMA, state='claimed', operation_id=operation,
        source=original, preview_fingerprint=expected_fingerprint,
        finance_acceptance=preview['prestate']['finance_pilot'])
    r._publish_once(client, 1623, r._claim_marker(operation), claim)
    try:
        # Recheck the entire stable preview after durable claim and immediately
        # before the only deploy. Capacity is rechecked by the canonical adapter.
        require(build_preview(client, target) == preview, 'pre-deploy-drift')
        canonical_deploy()
        require(r.prove_repo_only_descendant(client, original) == runner, 'final-main-drift')
        final = r.collect_prestate(target, PROFILE['merge_sha'], require_incomplete=False)
        require(final['finance_pilot'] == preview['prestate']['finance_pilot'], 'finance-drift')
        r._finish_deploy_protection(target, original)
        owner = collect_owner(target, source_proof(), complete=True)
        require(r.prove_repo_only_descendant(client, original) == runner, 'final-main-drift')
        result = dict(schema=r.RECOVERY_SCHEMA, state='complete', operation_id=operation,
            source=original, preview_fingerprint=expected_fingerprint, runner=runner,
            final_readback=final, final_owner=owner)
        r._publish_once(client, 1623, r._receipt_marker(operation), result)
        return result
    except Exception as exc:
        return dict(schema=r.RECOVERY_SCHEMA, state='ambiguous', operation_id=operation,
            source=original, preview_fingerprint=expected_fingerprint,
            reason=getattr(exc, 'reason', type(exc).__name__), recovery='readback-only; never repeat canonical deploy')
