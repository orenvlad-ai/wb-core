"""Single-use continuation of PR1615's proven unentered openrsync receiver.

This incident tool executes from outside the original clean released checkout.
Preview/readback are read-only. Apply creates a durable exclusive remote claim;
any subsequent invocation is readback-only, even after a deterministic failure.
It never changes the original release receipt, starts ownership, or resumes pause.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import io
import json
import os
import re
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tarfile
import textwrap

MERGE = '6c79dd35c209878065249e70beb474c8beee6d14'
BASE = '4dbb3f7b4b721714dcf84a39c80265e99f4215e1'
HEAD = 'c4d7756f32e631d94c886bb06093fc0616e88694'
OPERATION = 'release-v3-e6abed9778e2db9ef993d5f1f37ac041'
OWNER_FP = 'sha256:a0814d4c33e0bc2ba88c7afff65888a263f4b76c6e891edc48bf1617bac74331'
WINDOW = 'wbc0201-dispatch-valuation-20261111'
BASELINE = 'sha256:d6447b9a8e8695d9c47eb4d9ebd68a3d7277b1685607c0afd189161578858139'
LOG_HASH = '19d18eb4e6ed7d611cc0149ef67e8e251800357e79fea78d813eab469277f267'
RECEIPT_HASH = '773a17c6286f45b4287ab89f40cc1dd7f5c10f283f690e5c7d09b2db075316bf'
APP = '/opt/wb-core-runtime/app'
STATE = '/opt/wb-core-runtime/state'
ENV = '/opt/wb-ai/.env'
GNU_RSYNC = '/opt/homebrew/bin/rsync'
CLAIM = '.pr1615-openrsync-recovery.json'
JOURNAL = '.pr1615-openrsync-recovery-journal.jsonl'
TARGET_REL = 'artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json'
EXCLUDES = ('.git/', '.runtime/', 'scratch/', '.clasp.json', '__pycache__/',
            '.pytest_cache/', '.mypy_cache/', '.venv/', '.DS_Store')


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def excluded(relative, directory=False):
    parts = Path(relative).parts
    return any((part + '/' in EXCLUDES and (i < len(parts) - 1 or directory))
               or part in EXCLUDES for i, part in enumerate(parts))


def manifest(root):
    """lstat every affected entry; never follow excluded directories or symlinks."""
    result = {}
    def walk(directory):
        for entry in sorted(directory.iterdir()):
            relative = entry.relative_to(root).as_posix()
            info = entry.lstat()
            is_dir = stat.S_ISDIR(info.st_mode)
            if excluded(relative, is_dir):
                continue
            item = {'mode': stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                item.update(kind='symlink', target=os.readlink(entry))
            elif is_dir:
                item.update(kind='directory')
            elif stat.S_ISREG(info.st_mode):
                item.update(kind='file', size=info.st_size, sha256=digest(entry.read_bytes()))
            else:
                raise RuntimeError('unsupported-manifest-entry')
            result[relative] = item
            if is_dir:
                walk(entry)
    walk(Path(root))
    return result


def git(checkout, *args, binary=False):
    result = subprocess.run(['git', *args], cwd=checkout, check=True, capture_output=True)
    return result.stdout if binary else result.stdout.decode().strip()


def old_files(checkout, revision=BASE):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(git(checkout, 'archive', revision, binary=True))) as archive:
        for member in archive:
            name = member.name.rstrip('/')
            if excluded(name, member.isdir()) or member.isdir():
                continue
            if member.issym():
                result[name] = {'kind': 'symlink', 'target': member.linkname}
            else:
                require(member.isfile(), 'unsupported-git-entry')
                result[name] = {'kind': 'file', 'size': member.size,
                                'sha256': digest(archive.extractfile(member).read()),
                                'executable': bool(member.mode & 0o111)}
    return result


def verify_old(actual, expected):
    for name, item in expected.items():
        observed = actual.get(name, {})
        require(all((bool(observed.get('mode', 0) & 0o111) if key == 'executable'
                     else observed.get(key)) == value for key, value in item.items()),
                'old-tree-drift:' + name)
    # --delete is destructive: only known deploy-generated files may be extras.
    extras = {name for name, item in actual.items()
              if item['kind'] != 'directory' and name not in expected}
    require(all(name in {'.wb-core-runtime-sha', '.wb-core-deploy.json'}
                or name.startswith('packages/node/wb_autoanswers_v1_4_2/make_mvp/node_modules/')
                for name in extras),
            'unexpected-deletable-files')


def durable_create(path, payload):
    """Exclusive creation is the irreversible one-shot boundary, including crash."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        data = canonical(payload) + b'\n'
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def read_private(path, limit=1024 * 1024):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077
                and info.st_size <= limit, 'invalid-private-evidence')
        return os.read(fd, limit + 1).decode()
    finally:
        os.close(fd)


def marker_readback(path, *, json_value=False):
    try:
        text = path.read_text()
        return {'state': 'present', 'value': json.loads(text) if json_value else text.strip()}
    except FileNotFoundError:
        return {'state': 'absent'}
    except (OSError, ValueError):
        return {'state': 'unreadable-or-invalid'}


def remote_action(e):
    """Executed from trusted local source; imports only the installed helper."""
    sys.path.insert(0, APP)
    from packages.application import business_data_deploy_protection as deploy
    from packages.application import business_data_maintenance_pause as pause
    runtime = Path(STATE)
    claim_path = runtime / CLAIM
    journal_path = runtime / JOURNAL
    if e['action'] == 'readback':
        return {'claim': read_private(claim_path), 'journal': read_private(journal_path),
                'owner': deploy.load(runtime),
                'runtime_sha': marker_readback(Path(APP) / '.wb-core-runtime-sha'),
                'metadata': marker_readback(Path(APP) / '.wb-core-deploy.json', json_value=True)}
    if e['action'] == 'event':
        claim = json.loads(read_private(claim_path) or '{}')
        require(claim.get('fingerprint') == e['fingerprint'], 'claim-mismatch')
        fd = os.open(journal_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(canonical(e['event']) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'recorded': e['event']}
    with deploy._locked(runtime):
        require(read_private(claim_path) is None, 'existing-claim-readback-only')
        owner = deploy.require_owner(runtime, OPERATION, MERGE)
        require(owner['fingerprint'] == OWNER_FP and owner['phase'] == 'mutation_started'
                and owner['window_id'] == WINDOW and owner['baseline_fingerprint'] == BASELINE,
                'owner-changed')
        state = pause.load_state(runtime)
        _systemd, reader = deploy._clients(runtime, Path(ENV))
        quiet = reader()
        require(quiet.get('quiet') is True, 'pause-not-quiet')
        pause._unchanged(state, quiet)
        actual = manifest(Path(APP))
        verify_old(actual, e['old_files'])
        require((Path(APP) / '.wb-core-runtime-sha').read_text().strip() == BASE, 'runtime-changed')
        meta = json.loads((Path(APP) / '.wb-core-deploy.json').read_text())
        require(meta.get('commit') == BASE and meta.get('deployment_complete') is True, 'metadata-changed')
        proof = {'owner': owner, 'manifest': actual, 'metadata': meta,
                 'helper_sha256': actual['packages/application/business_data_deploy_protection.py']['sha256']}
        if e['action'] == 'claim':
            require(proof == e['prestate'], 'prestate-changed')
            durable_create(claim_path, {'schema': 'pr1615-openrsync-recovery/v1',
                'fingerprint': e['fingerprint'], 'operation': OPERATION, 'merge': MERGE,
                'prestate_sha256': digest(canonical(proof)), 'state': 'claimed'})
            # Revalidate after the durable boundary. Failure burns the claim.
            require(manifest(Path(APP)) == actual and deploy.require_owner(runtime, OPERATION, MERGE) == owner,
                    'postclaim-prestate-changed')
            current = reader()
            require(current.get('quiet') is True, 'postclaim-not-quiet')
            pause._unchanged(state, current)
        return proof


def remote_script(payload):
    constants = ('MERGE', 'BASE', 'OPERATION', 'OWNER_FP', 'WINDOW', 'BASELINE',
                 'APP', 'STATE', 'ENV', 'CLAIM', 'JOURNAL', 'EXCLUDES')
    source = 'import hashlib,json,os,stat,sys\nfrom pathlib import Path\n'
    source += '\n'.join(name + '=' + repr(globals()[name]) for name in constants) + '\n'
    source += '\n'.join(inspect.getsource(fn) for fn in
                        (require, digest, canonical, excluded, manifest, verify_old,
                         durable_create, read_private, marker_readback, remote_action))
    return source + '\nprint(json.dumps(remote_action(' + repr(payload) + '),sort_keys=True))\n'


def remote(hosted, target, payload):
    result = subprocess.run(hosted._remote_shell_command(target, 'python3 -'),
                            input=remote_script(payload), check=True, capture_output=True, text=True, timeout=180)
    return json.loads(result.stdout)


def canonical_stages(hosted, target, target_file):
    """Exact canonical deploy_current_checkout tail, starting at its failed sync."""
    commands = hosted.deploy_current_checkout(target, target_file=target_file,
                    dry_run=True, allow_dirty=False)['commands']
    storage = hosted._build_root_storage_policy_commands(target)
    pairs = [('sync', 'rsync'), ('chown', 'chown_target_dir'),
             ('systemd-barrier-preflight', 'systemd_barrier_preflight')]
    if commands['systemd_retire']:
        pairs += [('systemd-retire', 'systemd_retire'), ('daemon-reload', 'systemd_daemon_reload')]
    pairs += [('metadata', 'deploy_metadata'), ('root-storage-status', 'root_storage_status')]
    pairs += [('dependencies', name) for name in (
        'seller_portal_recovery_os_dependencies', 'seller_portal_owner_runtime_os_dependencies',
        'runtime_pip_install', 'seller_portal_recovery_venv', 'seller_portal_owner_runtime_venv',
        'seller_portal_owner_runtime_contract', 'seller_portal_recovery_playwright_browser',
        'buyer_chrome_install', 'autoanswers_os_dependencies', 'autoanswers_node_dependencies')]
    pairs += [('autoanswers-schema-preflight', 'autoanswers_prepare_deploy')]
    if commands['systemd_install']:
        pairs += [('systemd-install', 'systemd_install'), ('daemon-reload', 'systemd_daemon_reload')]
    pairs += [('nginx', 'nginx_public_routes_update'), ('restart', 'restart'), ('restart', 'systemd_reconcile')]
    if storage.get('release_bridge'):
        pairs += [('root-storage-status', 'root_storage_status')]
    pairs += [('readback', 'root_storage_status_artifact_readback'), ('status-readback', 'status'),
              ('readback', 'auth_env_preflight'), ('change-registry-activation', 'change_registry_activation'),
              ('readback', 'cleaner_precomplete_probe'), ('metadata-complete', 'deploy_completion_metadata')]
    return [(stage, commands[key]) for stage, key in pairs if commands[key]]


def command_fingerprint(stage, command):
    # Canonical builder timestamps are observational, not execution authority.
    # Preserve actual commands; normalize only their preview hash.
    value = canonical(command).decode()
    if stage in ('metadata', 'metadata-complete'):
        value = re.sub(r'deployed_at\\\":\\\"[0-9T:.Z+\-]+',
                       'deployed_at\\\":\\\"<build-time>', value)
    return digest(value.encode())


def bind_checkout(checkout, target_file):
    require((checkout / '.git').is_dir() and not (checkout / '.git').is_symlink(),
            'standalone-source-checkout-required')
    require(git(checkout, 'rev-parse', 'HEAD') == MERGE, 'source-not-exact-merge')
    require(git(checkout, 'rev-parse', 'HEAD^') == BASE, 'source-parent-changed')
    require(not git(checkout, 'status', '--porcelain'), 'source-dirty')
    require(target_file.resolve() == (checkout / TARGET_REL).resolve(), 'explicit-europe-target-required')
    # Import context is deliberately the released tree, never the recovery tree.
    sys.path.insert(0, str(checkout))
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    from apps import github_release_runner as release
    from packages.application import business_data_deploy_protection as deploy
    require(hosted.ROOT.resolve() == checkout.resolve()
            and Path(deploy.__file__).resolve().is_relative_to(checkout.resolve()), 'import-origin-mismatch')
    require(tuple(hosted.RSYNC_EXCLUDES) == EXCLUDES, 'exclusion-contract-changed')
    target = hosted.load_hosted_runtime_target(target_file)
    require((target.ssh_destination, target.target_dir, target.environment_file,
             target.runtime_env['REGISTRY_UPLOAD_RUNTIME_DIR']) == ('wb-core-eu-root', APP, ENV, STATE), 'target-mismatch')
    hosted._ensure_target_allows_mutation(target, action='deploy', dry_run=False)
    return hosted, release, deploy, target


def local_evidence(args):
    require(digest(args.log.read_bytes()) == LOG_HASH, 'failure-log-changed')
    require(digest(args.receipt.read_bytes()) == RECEIPT_HASH, 'original-receipt-changed')
    receipt = json.loads(args.receipt.read_text())
    require(receipt == {'base_sha': BASE, 'deployed_sha': None, 'gate_run_id': 43087855162,
        'head_sha': HEAD, 'merge_sha': MERGE, 'operation_id': OPERATION, 'pull_request': 1615,
        'reason': 'CalledProcessError', 'release_kind': 'live_runtime',
        'schema': 'wb-core.release-receipt/v3', 'state': 'blocked'}, 'receipt-identity')
    version = subprocess.run([GNU_RSYNC, '--version'], check=True, capture_output=True, text=True).stdout
    require(version.startswith('rsync  version 3.') and 'protocol version' in version, 'gnu-rsync-required')
    return {'original_release_run': 43088680396, 'original_receipt': receipt,
            'receipt_sha256': RECEIPT_HASH, 'log_sha256': LOG_HASH,
            'gnu_path': GNU_RSYNC, 'gnu_sha256': digest(Path(GNU_RSYNC).read_bytes()), 'gnu_version': version}


def run(args):
    hosted, release, deploy, target = bind_checkout(args.checkout.resolve(), args.target_file)
    existing = remote(hosted, target, {'action': 'readback'})
    if args.action == 'readback' or existing['claim'] is not None:
        return {'state': 'readback-only', **existing}
    evidence = local_evidence(args)
    identity = dict(app_dir=APP, runtime_dir=STATE, env_file=ENV, operation=OPERATION, expected_sha=MERGE)
    stages = canonical_stages(hosted, target, args.target_file)
    receiver = deploy.guarded_shell('exec rsync', receiver=True, **identity)
    stages[0] = ('sync', [GNU_RSYNC, '--rsync-path', receiver, *stages[0][1][1:]])
    # Prove the exact GNU shell quoting path without receiver writes.
    transport = [GNU_RSYNC, '--list-only', '--rsync-path', receiver,
                 '-e', shlex.join(hosted._ssh_command()), 'wb-core-eu-root:' + APP + '/.wb-core-runtime-sha']
    probe = subprocess.run(transport, check=True, capture_output=True, text=True, timeout=60)
    require('.wb-core-runtime-sha' in probe.stdout, 'transport-probe-unproven')
    for command in (hosted._build_auth_env_preflight_command(target),
                    hosted._build_root_storage_policy_commands(target).get('preflight')):
        if command:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    source_manifest = manifest(args.checkout.resolve())
    source_files = old_files(args.checkout, MERGE)
    verify_old(source_manifest, source_files)
    require({name for name, item in source_manifest.items() if item['kind'] != 'directory'}
            == set(source_files), 'unexpected-source-file')
    prestate = remote(hosted, target, {'action': 'preview', 'old_files': old_files(args.checkout)})
    preview = {'schema': 'pr1615-openrsync-recovery/v1', 'evidence': evidence,
               'target_sha256': digest(args.target_file.read_bytes()), 'source_manifest': source_manifest,
               'transport_command': transport, 'transport_stdout_sha256': digest(probe.stdout.encode()),
               'prestate': prestate,
               'deletions': sorted(set(prestate['manifest']) - set(source_manifest)),
               'stages': [{'stage': stage, 'command_sha256': command_fingerprint(stage, command)} for stage, command in stages]}
    fingerprint = digest(canonical(preview))
    preview['fingerprint'] = fingerprint
    if args.action == 'preview':
        return preview
    require(args.action == 'apply' and args.fingerprint == fingerprint, 'preview-fingerprint-changed')
    require(manifest(args.checkout.resolve()) == source_manifest, 'source-manifest-changed')
    receipt = {'schema': preview['schema'], 'fingerprint': fingerprint, 'operation': OPERATION,
               'merge': MERGE, 'state': 'ambiguous', 'completed_stages': []}
    def event(index, stage, state):
        remote(hosted, target, {'action': 'event', 'fingerprint': fingerprint,
                               'event': {'index': index, 'stage': stage, 'state': state}})
    try:
        # Claim dispatch itself can be indeterminate. Keep that uncertainty in
        # the recovery receipt; an existing claim always forces readback-only.
        remote(hosted, target, {'action': 'claim', 'fingerprint': fingerprint,
            'old_files': old_files(args.checkout), 'prestate': prestate})
        for index, (stage, command) in enumerate(stages):
            event(index, stage, 'before')
            if stage != 'sync':
                command = [*command[:-1], deploy.guarded_shell(command[-1], **identity)]
            if stage == 'status-readback':
                hosted._run_deploy_status_readback(command)
            else:
                hosted._run_command(command)
            event(index, stage, 'after')
            receipt['completed_stages'].append(stage)
        event(len(stages), 'external-runtime-readback', 'before')
        release.runtime_readback(json.loads(args.target_file.read_text()), MERGE)
        event(len(stages), 'external-runtime-readback', 'after')
        event(len(stages) + 1, 'owner-finish', 'before')
        subprocess.run(hosted._remote_shell_command(target, deploy.remote_shell('finish', **identity)),
                       check=True, timeout=120)
        event(len(stages) + 1, 'owner-finish', 'after')
        receipt['state'] = 'complete'
    except Exception as exc:
        receipt['error_code'] = type(exc).__name__
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview', 'apply', 'readback'))
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--target-file', type=Path, required=True)
    parser.add_argument('--log', type=Path, default=Path('/tmp/wbc0201-pr1615-deploy-continuation.log'))
    parser.add_argument('--receipt', type=Path, default=Path('/tmp/wbc0201-pr1615-release-receipt/release-receipt.json'))
    parser.add_argument('--fingerprint', default='')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    # Explicit path arguments control all target reads/builders; inherited legacy
    # TARGET selection must never affect downstream command construction.
    os.environ['WB_CORE_HOSTED_RUNTIME_TARGET_FILE'] = str(args.target_file.resolve())
    os.environ['WB_CORE_RELEASE_OPERATION_ID'] = OPERATION
    os.environ['WB_CORE_RELEASE_PR'] = '1615'
    os.environ['WB_CORE_RELEASE_HEAD'] = HEAD
    os.environ['WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH'] = 'true'
    try:
        result = run(args)
    except Exception as exc:
        result = {'state': 'blocked', 'error_code': type(exc).__name__, 'reason': str(exc)[:250]}
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    args.output.chmod(0o600)
    print(json.dumps({'state': result.get('state', 'preview'), 'fingerprint': result.get('fingerprint'),
                      'output': str(args.output)}))
    return 0 if result.get('state') in (None, 'complete', 'readback-only') else 1


if __name__ == '__main__':
    sys.exit(main())
