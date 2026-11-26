"""Provenance for the final cleaner check of a single prior pre-sync recovery."""
from __future__ import annotations

from ci import post_merge_release_recovery as r
from ci import pr1623_presync_recovery as prior

ORIGINAL_RUN = 45797154129
RUN = 45800208191
RUNNER = '8a795be247f4c1a7650a0a829bc196cb7b47d5e7'
JOB = 139841167405
ARTIFACT = 15106493486
CLAIM_COMMENT = 7189225105
PREVIEW = '2633e904b70b2aaa1d5db90181b2970e38a4cb56c0fe4e53c9bb91ffec258130'
ARCHIVE_SHA = '45c7f405e675434e104c22265cea9f5c3552d3c0fddea1268a7d53adb46bc63c'
LOG_SHA = '84dec7e0a8e20f0c1c6a86e6ae40923d6c429ad7f0c1cdd8a4c86dd7648e1630'
FINANCE_SHA = '4534efc3b7534b6ab7f0b8f707a0ada63a69da44dd071d12c58d511b0f0ab1d2'
STARTED_OWNER = 'sha256:66899902d573b67ff37f84595404731721358374e727100c2607a00148fad6ef'
PROFILE = prior.PRESYNC_PROFILES[ORIGINAL_RUN]
JOB_NAME = 'Proven post-merge deploy-tail recovery'
STEP_NAME = 'Validate evidence and preview or continue only the proven tail'


def require(value, reason):
    if not value:
        raise r.RecoveryError('pr1635-cleaner-' + reason)


def prove_job(raw, gate, job_name):
    profile = PROFILE['PROFILE']
    require(gate == profile['gate_run_id'] and job_name == JOB_NAME and r.digest(raw) == LOG_SHA, 'job-binding')
    lines = raw.decode().splitlines()
    markers = ('{"status": "skipped_maintenance", "reason": "skipped_maintenance"}',
               'line 30, in _listener_ready',
               'RuntimeError: cleaner listener not ready for before_complete',
               'line 1285, in deploy_current_checkout',
               'run_stage("readback",cleaner_precomplete_probe_command,allow_transport_reconciliation=False)',
               'subprocess.CalledProcessError: Command ')
    positions = [[i for i, line in enumerate(lines) if marker in line] for marker in markers]
    require(all(len(pos) == 1 for pos in positions) and positions == sorted(positions), 'stage-order')
    failure = lines[positions[-1][0]]
    require(all(s in failure for s in ('apps/search_cluster_cleaner_release_probe.py', '--phase before_complete',
            '--expected-sha ' + profile['merge_sha'], profile['operation_id'], 'returned non-zero exit status 1.')),
            'failure-command')
    return dict(stage='cleaner-before-complete-after-skipped-activation', job_name=job_name,
                job_log_sha256=LOG_SHA, exit_status=1)


def collect(client):
    require(client.repository == r.REPOSITORY, 'repository')
    first = prior.evidence(client, release_run_id=ORIGINAL_RUN)
    original = first['original_receipt']
    operation = r.recovery_operation_id(ORIGINAL_RUN, original)
    run = client.get(f'/actions/runs/{RUN}')
    require((run['id'], run['name'], run['path'], run['event'], run['run_attempt'], run['head_sha'],
             run['head_branch'], run['status'], run['conclusion']) == (RUN, 'Post-merge Release Recovery',
             '.github/workflows/release-recovery.yml', 'workflow_dispatch', 1, RUNNER, 'main', 'completed', 'failure')
             and all(run[k]['full_name'] == r.REPOSITORY for k in ('repository', 'head_repository')), 'parent-run')
    artifacts = client.get(f'/actions/runs/{RUN}/artifacts?per_page=100')['artifacts']
    require(len(artifacts) == 1, 'artifact-count')
    a = artifacts[0]
    require((a['id'], a['workflow_run']['id'], a['name'], a['expired'], a['digest']) ==
            (ARTIFACT, RUN, f'release-recovery-{ORIGINAL_RUN}-{RUN}', False, 'sha256:' + ARCHIVE_SHA), 'artifact')
    archive = client.request('GET', f'/actions/artifacts/{ARTIFACT}/zip', raw=True)
    require(r.digest(archive) == ARCHIVE_SHA, 'archive-digest')
    files = r._zip_files(archive, 'pr1635-cleaner-parent')
    require(set(files) == {'recovery-receipt.json'}, 'archive-members')
    receipt = r._json_file(files, 'recovery-receipt.json', 'pr1635-cleaner-parent')
    require(receipt == dict(schema=r.RECOVERY_SCHEMA, state='ambiguous', operation_id=operation, source=original,
            preview_fingerprint=PREVIEW, reason='CalledProcessError', recovery='readback-only; never repeat canonical deploy'), 'receipt')
    marker = r._claim_marker(operation)
    comment = client.get(f'/issues/comments/{CLAIM_COMMENT}')
    require(comment['id'] == CLAIM_COMMENT and comment['user']['login'] == 'github-actions[bot]'
            and comment['user']['id'] == 41898282 and comment['performed_via_github_app']['slug'] == 'github-actions',
            'claim-author')
    claim = r._comment_payload(comment['body'], marker)
    require(isinstance(claim, dict) and claim['source'] == original and claim['operation_id'] == operation
            and claim['state'] == 'claimed' and claim['preview_fingerprint'] == PREVIEW
            and r._matching_comments(client, original['pull_request'], marker) == [claim], 'claim')
    verify_finance(claim['finance_acceptance'])
    jobs = client.get(f'/actions/runs/{RUN}/jobs?filter=latest&per_page=100')['jobs']
    require(len(jobs) == 1 and (jobs[0]['id'], jobs[0]['name'], jobs[0]['status'], jobs[0]['conclusion'])
            == (JOB, JOB_NAME, 'completed', 'failure') and
            [s['name'] for s in jobs[0]['steps'] if s['conclusion'] == 'failure'] == [STEP_NAME], 'parent-job')
    raw = client.request('GET', f'/actions/jobs/{JOB}/logs', raw=True)
    proof = prove_job(raw, original['gate_run_id'], JOB_NAME)
    proof.update(parent_run=ORIGINAL_RUN, failed_recovery=RUN, parent_operation=operation, parent_preview=PREVIEW,
                 parent_claim_id=CLAIM_COMMENT, parent_archive_sha256=ARCHIVE_SHA, parent_finance_sha256=FINANCE_SHA,
                 original_failure=first['failure'])
    return {**first, 'release_run_id': RUN, 'failure': proof}


def verify_finance(state):
    require(r.digest(r.canonical_bytes(state)) == FINANCE_SHA, 'finance-drift')


def verify_owner(target, *, complete=False):
    hashes = prior.source_proof(release_run_id=ORIGINAL_RUN)
    for path in ('apps/search_cluster_cleaner_release_probe.py', 'apps/change_registry_observer.py'):
        hashes[path] = r.digest(r._git(['show', PROFILE['PROFILE']['merge_sha'] + ':' + path]).stdout.encode())
    proof = prior.collect_owner(target, hashes, complete=complete, finishing=not complete, release_run_id=ORIGINAL_RUN)
    owner = proof['owner']
    require(owner['phase'] == 'complete' if complete else owner['phase'] in {'mutation_started','complete'}, 'owner-phase')
    if owner['phase'] == 'mutation_started':
        require(owner['fingerprint'] == STARTED_OWNER, 'started-owner')
    return owner


def verify_before(target, state, started):
    verify_finance(state['finance_pilot'])
    require(started and started['quiet'] and not started['baseline_unit_changes'] and
            started['owner']['phase'] == 'mutation_started' and started['owner'] == verify_owner(target), 'held-owner')
    prior.storage_preflight(target)
