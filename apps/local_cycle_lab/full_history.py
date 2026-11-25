"""Disposable Linux History provisioning and immutable native-store readback.

No native admission function is replaced. The guest must have a real mounted
filesystem, systemd service definitions, and an isolated network namespace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MOUNT = Path('/mnt/wb-core-extra100')


def isolation_evidence() -> dict:
    if sys.platform != 'linux':
        raise RuntimeError('full History requires a real Linux guest')
    interfaces = sorted(name for _, name in socket.if_nameindex())
    if interfaces != ['lo']:
        raise RuntimeError('lab process must run in a loopback-only private network namespace')
    if os.geteuid() == 0:
        raise RuntimeError('lab application must run as an unprivileged guest account')
    # Check the actual kernel, not an injected Python network shim.
    route = Path('/proc/net/route').read_text()
    if len(route.splitlines()) != 1:
        raise RuntimeError('private network namespace unexpectedly has routes')
    return {'kernel': os.uname().release, 'architecture': os.uname().machine,
            'uid': os.geteuid(), 'interfaces': interfaces,
            'network_namespace': os.readlink('/proc/self/ns/net'),
            'host_mounts': [line for line in Path('/proc/self/mountinfo').read_text().splitlines()
                            if ' - virtiofs ' in line or ' - 9p ' in line]}


def prepare_history(runtime_dir: Path, candidate_root: Path | None = None):
    """Pin this disposable source build to its real guest storage contract."""
    from apps.web_vitrina_history_candidate_build import runtime_storage_admission
    from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig
    evidence = isolation_evidence()
    if evidence['host_mounts']:
        raise RuntimeError('lab guest must not mount host directories')
    runtime_dir = Path(runtime_dir).resolve()
    suffix = hashlib.sha256(str(runtime_dir).encode()).hexdigest()[:16]
    root = Path(candidate_root) if candidate_root else MOUNT / 'local-history' / suffix
    root = root.resolve()
    if not root.is_relative_to(MOUNT) or root == MOUNT or root.is_relative_to(runtime_dir):
        raise ValueError('History candidate must be separate on the disposable mount')
    contract = json.loads((ROOT / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
    contract['candidate_root'] = str(root)
    contract['mount'] = subprocess.check_output(
        ['findmnt', '-n', '-T', str(MOUNT), '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'],
        text=True, timeout=2).strip()
    # Preserve the reviewed native hashes, formula epoch and full reserve.
    # runtime_storage_admission below verifies every source file against them.
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / 'runtime-contract.json'
    if path.exists() and json.loads(path.read_text()) != contract:
        raise RuntimeError('existing local History contract differs; use a fresh candidate root')
    if not path.exists():
        path.write_text(json.dumps(contract, indent=2) + '\n')
        path.chmod(0o600)
    runtime_storage_admission(root, path, contract['formula_epoch'])
    for name in ('.web-vitrina-finished-builder.lock', '.wb-finance-daily-worker.lock'):
        (runtime_dir / name).touch(mode=0o600, exist_ok=True)
    return CycleHistoryConfig(candidate_root=root, runtime_contract=path,
                              formula_epoch=contract['formula_epoch'])


def published_history(config):
    """Open the current edition after checking the reviewed storage contract."""
    from apps.web_vitrina_history_candidate_build import runtime_storage_admission
    from packages.application.web_vitrina_history_store import HistoryStore
    from packages.application.web_vitrina_history_compiler import digest
    reviewed = json.loads((ROOT / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
    actual = json.loads(config.runtime_contract.read_text())
    for key in ('formula_code_hashes', 'formula_epoch', 'reserve_bytes'):
        if actual[key] != reviewed[key]:
            raise RuntimeError('local History changed the reviewed native contract')
    runtime_storage_admission(Path(config.candidate_root), config.runtime_contract, config.formula_epoch)
    store = HistoryStore(Path(config.candidate_root) / 'history')
    pointer = json.loads((store.root / 'CURRENT.json').read_text())['current']
    edition = store.edition(pointer)
    if pointer != digest(edition):
        raise RuntimeError('History edition digest differs from published pointer')
    return store, pointer, edition
