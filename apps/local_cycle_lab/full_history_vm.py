"""Host-side transport to this task's disposable Linux VM, never production.

The Lima binary is task-local. Its state uses a short task-specific /tmp path
because macOS Unix-domain socket paths cannot hold the full worktree path.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
LIMA = Path(os.environ.get('WBC_LAB_LIMA_BINARY', str(ROOT / 'output/stage4-vm/tool/bin/limactl')))
LIMA_HOME = os.environ.get('WBC_LAB_LIMA_HOME', '/tmp/wbc-cycle-lab-stage4')
INSTANCE = os.environ.get('WBC_LAB_LIMA_INSTANCE', 's4')


def lima(*args, **kwargs):
    return subprocess.run([str(LIMA), *args], env={**os.environ, 'LIMA_HOME': LIMA_HOME},
                          check=True, **kwargs)


def guest(*args, **kwargs):
    return lima('shell', '--workdir=/tmp', INSTANCE, *args, **kwargs)


def sync_source():
    # Only tracked application/fixture source and this lab's current Python
    # files. No git metadata, production runtime, env files, tokens or control.
    tracked = subprocess.check_output(['git', 'ls-files', '-z', 'apps', 'packages', 'artifacts'], cwd=ROOT).decode().split('\0')
    files = {ROOT / name for name in tracked if name}
    files.update((ROOT / 'apps/local_cycle_lab').glob('*.py'))
    archive = ROOT / 'output/stage4-vm/source.tgz'
    fingerprint = hashlib.sha256()
    with tarfile.open(archive, 'w:gz') as out:
        for path in sorted(files):
            if path.is_file() and not path.is_symlink():
                name = str(path.relative_to(ROOT))
                with path.open('rb') as source:
                    fingerprint.update(name.encode() + b'\0' + hashlib.file_digest(source, 'sha256').digest())
                    source.seek(0)
                    out.addfile(out.gettarinfo(str(path), arcname=name), source)
        identity = json.dumps({'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                               'tree_content_sha256': fingerprint.hexdigest()}).encode()
        info = tarfile.TarInfo('local-lab-source.json')
        info.size = len(identity)
        out.addfile(info, io.BytesIO(identity))
    with archive.open('rb') as stream:
        guest('sudo', 'tar', '-xzf', '-', '-C', '/opt/wbc-lab', stdin=stream)


def isolated(module: str, arguments: list[str]):
    clock = []
    if module == 'apps.local_cycle_lab.full_cycle':
        # Date-aware domain writers and the owned History child also call the
        # system clock directly. Shift only this unit's realtime, including its
        # descendants; leave elapsed time, filesystem times and the VM untouched.
        day = json.loads((ROOT / 'apps/local_cycle_lab/scenario.json').read_text())['today']
        clock = ['/usr/bin/datefudge', day + ' 08:00:00 UTC']
    return guest('sudo', 'systemd-run', '--quiet', '--wait', '--pipe', '--collect',
        '--unit=wbc-lab-' + uuid.uuid4().hex, '--uid=wbc-lab', '--gid=wbc-lab',
        '--working-directory=/opt/wbc-lab',
        '--property=PrivateNetwork=yes', '--property=NoNewPrivileges=yes',
        '--property=CapabilityBoundingSet=', '--property=ProtectSystem=strict',
        '--property=ProtectHome=yes',
        '--property=ReadWritePaths=/var/lib/wbc-lab /mnt/wb-core-extra100',
        '--property=PrivateTmp=yes',
        '--setenv=PLAYWRIGHT_BROWSERS_PATH=/opt/wbc-lab-browsers',
        '--setenv=PYTHONUNBUFFERED=1', *clock,
        '/opt/wbc-lab-venv/bin/python', '-m', module, *arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('sync', 'run', 'shell'))
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == 'sync':
        sync_source()
    elif args.action == 'run':
        if not args.arguments or not args.arguments[0].startswith('apps.local_cycle_lab.'):
            parser.error('run requires an apps.local_cycle_lab module name')
        isolated(args.arguments[0], args.arguments[1:])
    else:
        guest(*args.arguments)


if __name__ == '__main__':
    main()
