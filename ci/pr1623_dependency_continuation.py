"""Exact continuation after the PR1623 Chrome RAM guard, without reinstalling it."""
from __future__ import annotations
import json
from pathlib import Path
import re
import shlex
import subprocess

from ci import post_merge_release_recovery as r
from ci import pr1623_presync_recovery as prior

FAILED_RUN = 45473903471
PARENT = 'release-recovery-v1-bc03bf4f5f655504e76c796694fd3eb2'
OPERATION = PARENT + '-dependency-continuation-45473903471'
FINGERPRINT = '09a09b6dbbcb7eb164137b73914ffc959e0f57ee8a844d4e347bc5a4ba5e1924'
RUNNER = 'be9088fe1edef8e312a2555f5302110582f5c014'
MERGE = prior.PROFILE['merge_sha']


def require(value, reason):
    if not value: raise r.RecoveryError('pr1623-dependency-' + reason)


def prove_failure(raw):
    text = raw.decode('utf-8', errors='replace')
    required = ('line 1242, in deploy_current_checkout',
        'run_stage("dependencies", buyer_chrome_install_command)',
        'line 151, in install', 'raise RuntimeError("insufficient RAM for Chrome staging")',
        'RuntimeError: insufficient RAM for Chrome staging', 'python3 apps/wb_buyer_chrome_runtime.py',
        prior.PROFILE['operation_id'], '--expected-sha ' + MERGE)
    failures = re.findall(r'(?:subprocess\.)?CalledProcessError:.*?returned non-zero exit status (\d+)', text, re.DOTALL)
    require(all(s in text for s in required) and failures == ['1'] and 'exit status 255' not in text,
        'not-exact-unentered-chrome-ram-guard')
    return r.digest(raw)


def parent_evidence(client):
    require(client.repository==r.REPOSITORY,'repository')
    original = prior.evidence(client)['original_receipt']
    run = client.get(f'/actions/runs/{FAILED_RUN}')
    require(tuple(run.get(k) for k in ('name','path','event','run_attempt','status','conclusion','head_sha')) ==
        ('Post-merge Release Recovery','.github/workflows/release-recovery.yml','workflow_dispatch',1,'completed','failure',RUNNER), 'parent-run')
    artifacts = client.get(f'/actions/runs/{FAILED_RUN}/artifacts?per_page=100').get('artifacts', [])
    artifacts = [a for a in artifacts if a.get('name') == f'release-recovery-{prior.RUN}-{FAILED_RUN}' and not a.get('expired')]
    require(len(artifacts) == 1, 'parent-artifact')
    raw = client.request('GET', f"/actions/artifacts/{int(artifacts[0]['id'])}/zip", raw=True)
    receipt = r._json_file(r._zip_files(raw, 'pr1623-dependency-parent'), 'recovery-receipt.json', 'pr1623-dependency-parent')
    require(receipt == dict(schema=r.RECOVERY_SCHEMA, state='ambiguous', operation_id=PARENT,
        preview_fingerprint=FINGERPRINT, reason='CalledProcessError',
        recovery='readback-only; never repeat canonical deploy', source=original), 'parent-receipt')
    claims = r._matching_comments(client, 1623, r._claim_marker(PARENT))
    require(len(claims) == 1 and claims[0].get('state') == 'claimed' and claims[0].get('operation_id') == PARENT
        and claims[0].get('source') == original and claims[0].get('preview_fingerprint') == FINGERPRINT
        and isinstance(claims[0].get('finance_acceptance'), dict), 'parent-claim')
    jobs = client.get(f'/actions/runs/{FAILED_RUN}/jobs?filter=latest&per_page=100').get('jobs', [])
    require(len(jobs) == 1 and jobs[0].get('name') == 'Proven post-merge deploy-tail recovery'
        and jobs[0].get('status') == 'completed' and jobs[0].get('conclusion') == 'failure'
        and [s.get('name') for s in jobs[0].get('steps', []) if s.get('conclusion') == 'failure'] ==
            ['Validate evidence and preview or continue only the proven tail'], 'parent-job')
    log = client.request('GET', f"/actions/jobs/{int(jobs[0]['id'])}/logs", raw=True)
    return dict(source=original, finance=claims[0]['finance_acceptance'],
        receipt_sha256=r.digest(r.canonical_bytes(receipt)), job_log_sha256=prove_failure(log))


def source_proof():
    hashes = prior.source_proof()
    for path in ('apps/wb_buyer_chrome_runtime.py', 'apps/search_cluster_cleaner_release_probe.py'):
        hashes[path] = r.digest(subprocess.run(['git','show',MERGE+':'+path], cwd=r.ROOT, check=True, capture_output=True).stdout)
    adapter = r._git(['show',MERGE+':apps/registry_upload_http_entrypoint_hosted_runtime.py']).stdout.splitlines()
    chrome = r._git(['show',MERGE+':apps/wb_buyer_chrome_runtime.py']).stdout.splitlines()
    require(adapter[1241].strip() == 'run_stage("dependencies", buyer_chrome_install_command)'
        and adapter[1242].strip() == 'run_stage("dependencies", autoanswers_os_dependencies_command)'
        and chrome[150].strip() == 'raise RuntimeError("insufficient RAM for Chrome staging")'
        and chrome[151].strip() == 'ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)', 'source-stage')
    return hashes


def dependency_script(target):
    source = r._selective_live_contract_script(target, merge=MERGE, autoanswers_timer_state='disabled')
    extra = r._buyer_chrome_native_validator_source() + '''
for tool in ('ffmpeg','zstd','psql','xvfb-run','Xvfb','x11vnc','websockify','openbox'):
    subprocess.run(['sh','-c','command -v '+tool],check=True,capture_output=True)
subprocess.run(['systemctl','is-active','--quiet','postgresql'],check=True)
if not Path('/usr/share/novnc').is_dir(): raise SystemExit(47)
playwright=Path('/root/.cache/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/chrome-headless-shell')
if not playwright.is_file() or 'not found' in subprocess.run(['ldd',str(playwright)],check=True,text=True,capture_output=True).stdout: raise SystemExit(48)
package=Path(e['target'])/'packages/node/wb_autoanswers_v1_4_2/make_mvp'
module_dir=package/'node_modules'
if module_dir.is_symlink(): raise SystemExit(46)
modules=None
if module_dir.exists():
    if not module_dir.is_dir(): raise SystemExit(46)
    modules=subprocess.run(['node','-e',"require('ajv');require('ajv-formats');console.log(require('ajv/package.json').version,require('ajv-formats/package.json').version)"],cwd=package,check=True,text=True,capture_output=True).stdout.strip()
    if modules!='8.17.1 3.0.1': raise SystemExit(46)
versions['npm_modules']={'installed':modules is not None,'versions':modules}
versions['installed_buyer_chrome']=verify_installed_buyer_chrome(Path(e['target']))
# The installer adds only this marker outside the verified package tree.
import os,tarfile
from pathlib import PurePosixPath
package=Path('/opt/wb-core-runtime/wb-buyer-chrome/google-chrome-stable_154.0.8037.57-1_amd64.deb')
runtime=Path('/dev/shm/wb-buyer-chrome-154.0.8037.57-1')
expected={'.package-sha256'}
with subprocess.Popen(['dpkg-deb','--fsys-tarfile',str(package)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as process:
    with tarfile.open(fileobj=process.stdout,mode='r|') as archive:
        for member in archive:
            name=str(PurePosixPath(member.name))
            if name!='.': expected.add(name)
            if len(expected)>10000: raise RuntimeError('buyer-chrome-tree-bound')
    if process.wait(timeout=15)!=0: raise RuntimeError('buyer-chrome-tree-unavailable')
actual=set(); pending=[runtime]
while pending:
    with os.scandir(pending.pop()) as entries:
        for entry in entries:
            actual.add(str(Path(entry.path).relative_to(runtime)))
            if len(actual)>10000: raise RuntimeError('buyer-chrome-tree-bound')
            if entry.is_dir(follow_symlinks=False): pending.append(Path(entry.path))
if actual!=expected: raise RuntimeError('buyer-chrome-tree-extras-or-missing')
'''
    offset = source.rindex('print(json.dumps(')
    return source[:offset] + extra + source[offset:]


def collect(target, parent, *, complete=False):
    state = r.collect_prestate(target, MERGE, require_incomplete=not complete)
    require(state['finance_pilot'] == parent['finance'], 'finance-changed')
    hashes = source_proof()
    owner = prior.collect_owner(target, hashes, complete=complete, finishing=not complete)
    require(owner['window'] == prior.WINDOW and owner['baseline'] == prior.BASELINE, 'owner-window')
    if not complete: require(owner['owner']['phase'] == 'mutation_started', 'owner-phase')
    state['dependency_closure'] = r._run_remote_json(target, dependency_script(target))
    if complete:
        require(state['dependency_closure']['dependency_versions']['npm_modules']==
            {'installed':True,'versions':'8.17.1 3.0.1'},'npm-incomplete')
    # A full exact installed closure replaces an unnecessary installer, never
    # its resource predicate for new installation. No Chrome process is started.
    state['owner'] = owner
    if not complete:
        state['started_owner'] = r.collect_existing_started_owner(target, parent['source'])
        require(state['started_owner'] is not None, 'started-owner')
    prior.storage_preflight(target)
    return state


def build_preview(client, target):
    parent = parent_evidence(client)
    require(target.target_id == 'wb_core_eu_hosted_runtime_active', 'target')
    result = dict(schema=r.RECOVERY_SCHEMA, state='reviewable', mode='preview', release_run_id=FAILED_RUN,
        operation_id=OPERATION, source=parent['source'], parent=parent,
        runner=r.prove_repo_only_descendant(client,parent['source']), prestate=collect(target,parent),
        stages=['verified-installed-dependency-closure','canonical-prepare-systemd-nginx-restart-tail',
            'canonical-storage-status-auth-activation','cleaner-before-complete','metadata-completion-cas','native-owner-finish'],
        forbidden_stages=['merge','sync','chown','os-python-browser-install','browser-launch','resume','cycle'])
    if not result['prestate']['dependency_closure']['dependency_versions']['npm_modules']['installed']:
        result['stages'].insert(1,'absent-locked-npm-dependencies-once-and-readback')
    result['preview_fingerprint'] = prior.fingerprint(result)
    return result


def commands(target, state):
    result = r.build_stage_commands(target, MERGE, state['metadata_sha256'], state['main_pid'],
        case=r.RecoveryCase.WORKER_HEALTH_PRECHECK_ACTIVATION)
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    result['cleaner'] = hosted._remote_shell_command(target,
        f'cd {shlex.quote(target.target_dir)} && /usr/bin/python3 apps/search_cluster_cleaner_release_probe.py '
        f'--phase before_complete --expected-sha {MERGE} '
        f'--runtime-dir {shlex.quote(target.runtime_env["REGISTRY_UPLOAD_RUNTIME_DIR"])}')
    result['npm']=hosted._build_autoanswers_node_dependencies_command(target)
    return result


def run(client, target, command, expected_fingerprint):
    require(command in {'preview','apply'}, 'command')
    parent = parent_evidence(client)
    runner = r.prove_repo_only_descendant(client, parent['source'])
    claims = r._matching_comments(client,1623,r._claim_marker(OPERATION))
    receipts = r._matching_comments(client,1623,r._receipt_marker(OPERATION))
    if claims or receipts:
        require(len(claims)==1 and claims[0].get('source')==parent['source']
            and claims[0].get('parent')==parent and claims[0].get('state')=='claimed', 'existing-claim')
        if command=='apply': require(claims[0].get('preview_fingerprint')==expected_fingerprint,'existing-fingerprint')
        if receipts:
            require(len(receipts)==1 and receipts[0].get('source')==parent['source']
                and receipts[0].get('preview_fingerprint')==claims[0]['preview_fingerprint']
                and receipts[0].get('state')=='complete','existing-receipt')
        try:
            # Complete marker readback is the only path after a consumed claim.
            final = r.collect_prestate(target,MERGE,require_incomplete=False)
            require(final['finance_pilot']==parent['finance'],'finance-changed')
            prior.collect_owner(target,source_proof(),finishing=True)
            if command=='preview':
                return dict(schema=r.RECOVERY_SCHEMA,state='reviewable',operation_id=OPERATION,
                    preview_fingerprint=claims[0]['preview_fingerprint'],stages=['readback-only-finish-and-receipt'])
            require(r.prove_repo_only_descendant(client,parent['source'])==runner,'main-drift')
            r._finish_deploy_protection(target,parent['source'])
            final=collect(target,parent,complete=True)
            require(r.prove_repo_only_descendant(client,parent['source'])==runner,'main-drift')
            if receipts: return {**receipts[0],'fresh_final_readback':final}
            result=dict(schema=r.RECOVERY_SCHEMA,state='complete',operation_id=OPERATION,source=parent['source'],
                preview_fingerprint=claims[0]['preview_fingerprint'],parent=parent,final_readback=final)
            r._publish_once(client,1623,r._receipt_marker(OPERATION),result)
            return result
        except Exception as exc:
            return dict(schema=r.RECOVERY_SCHEMA,state='ambiguous',operation_id=OPERATION,reason=getattr(exc,'reason',type(exc).__name__))
    preview=build_preview(client,target)
    if command=='preview': return preview
    require(preview['preview_fingerprint']==expected_fingerprint,'fingerprint')
    claim=dict(schema=r.RECOVERY_SCHEMA,state='claimed',operation_id=OPERATION,source=parent['source'],
        parent=parent,preview_fingerprint=expected_fingerprint)
    r._publish_once(client,1623,r._claim_marker(OPERATION),claim)
    stages=[]
    try:
        require(build_preview(client,target)==preview,'post-claim-drift')
        state=preview['prestate']; cmd=commands(target,state); tail=cmd['normal_activation_tail']
        def stage(name, command, **kwargs):
            r._publish_once(client,1623,r._phase_marker(OPERATION,'before-'+name),
                dict(state='before',operation_id=OPERATION,phase=name,preview_fingerprint=expected_fingerprint))
            protected=r._protect_deploy_command(target,parent['source'],command)
            stages.append(r._bounded_status_readback(protected) if name=='managed-service-status'
                else r._must_succeed(name,protected,**kwargs))
            r._publish_once(client,1623,r._phase_marker(OPERATION,'after-'+name),
                dict(state='after',operation_id=OPERATION,phase=name,preview_fingerprint=expected_fingerprint))
        if not state['dependency_closure']['dependency_versions']['npm_modules']['installed']:
            prior.storage_preflight(target)
            stage('locked-npm-dependencies',cmd['npm'])
            installed=collect(target,parent)
            before=json.loads(json.dumps(state)); after=json.loads(json.dumps(installed))
            before['dependency_closure']['dependency_versions'].pop('npm_modules')
            modules=after['dependency_closure']['dependency_versions'].pop('npm_modules')
            require(before==after and modules=={'installed':True,'versions':'8.17.1 3.0.1'},'npm-readback-drift')
            state=installed
        phases=(('auth',tail['auth']),('storage-status',tail['storage']),('barrier',tail['barrier']),
            ('autoanswers-prepare-deploy',tail['prepare']),('systemd-install',tail['install']),('daemon-reload',tail['daemon_reload']),
            ('nginx',tail['nginx']),('restart',tail['restart']),('systemd-reconcile',tail['reconcile']),
            ('post-storage-status',tail['storage']),('storage-readback',tail['storage_readback']),
            ('managed-service-status',tail['status']),('post-auth',tail['auth']))
        for name, command in phases:
            prior.storage_preflight(target)
            stage(name,command)
        after=collect(target,parent)
        before=deepcopy_without_processes(state); stable=deepcopy_without_processes(after)
        require(before==stable and after['main_pid']>0 and after['main_pid']!=state['main_pid'],'post-restart-drift')
        cmd=commands(target,after)
        stage('activation',cmd['activation'])
        stage('cleaner-before-complete',cmd['cleaner'])
        require(collect(target,parent)==after,'before-completion-drift')
        require(r.prove_repo_only_descendant(client,parent['source'])==runner,'main-drift')
        stage('metadata-complete',cmd['completion'],input_text=cmd['completion_input'])
        final=r.collect_prestate(target,MERGE,require_incomplete=False)
        require(final['finance_pilot']==parent['finance'],'finance-changed')
        require(r.prove_repo_only_descendant(client,parent['source'])==runner,'main-drift')
        r._finish_deploy_protection(target,parent['source'])
        final=collect(target,parent,complete=True)
        require(r.prove_repo_only_descendant(client,parent['source'])==runner,'main-drift')
        result=dict(schema=r.RECOVERY_SCHEMA,state='complete',operation_id=OPERATION,source=parent['source'],
            parent=parent,preview_fingerprint=expected_fingerprint,final_readback=final,completed_stages=stages)
        r._publish_once(client,1623,r._receipt_marker(OPERATION),result)
        return result
    except Exception as exc:
        return dict(schema=r.RECOVERY_SCHEMA,state='ambiguous',operation_id=OPERATION,source=parent['source'],
            preview_fingerprint=expected_fingerprint,reason=getattr(exc,'reason',type(exc).__name__),completed_stages=stages)


def deepcopy_without_processes(value):
    value=json.loads(json.dumps(value))
    for key in ('main_pid','pilot_pid'): value.pop(key,None)
    return value
