"""Zstandard storage for verified Finance sets; never writes live databases.

The existing manifest continues to describe the restored bytes. A separate,
manifest-bound receipt describes stored bytes. Old uncompressed sets stay valid.
Only unselected partial sets may be compacted, and each archive is decoded and
hashed before its staging original is released. A disk restore/hash is required
before publication. No compression ratio is assumed for admission.
"""
import hashlib
import json
import re
import os
from pathlib import Path
import shutil
import subprocess

from packages.application.finance_storage_snapshot_retention import (
    _atomic_write_json, _fingerprint, _fsync_directory, _sha256_file,
    FinanceStorageSnapshotRetentionError,
)

DBS = ('finance_raw.sqlite3', 'operational.sqlite3')
RECEIPT = 'compression.json'
FORMAT = 'zstd-per-file-v1'
OVERHEAD = 1024 * 1024


def require_codec():
    if not shutil.which('zstd'):
        raise FinanceStorageSnapshotRetentionError('zstd is required before backup compression')


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageSnapshotRetentionError('unsafe compressed backup file: ' + str(path))


def _space(path, reserve, required=0):
    if shutil.disk_usage(path).free < reserve + required:
        raise FinanceStorageSnapshotRetentionError('compressed backup staging reserve exhausted')


def _run(command, *, output=None, expected=None, limit=None, reserve=0, root=None):
    """Bounded streaming, including decoded-length and SHA checks. No shell."""
    digest = hashlib.sha256(); count = 0
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if limit is not None and count > limit:
                    raise FinanceStorageSnapshotRetentionError('compressed backup byte budget exceeded')
                if output is not None:
                    _space(root, reserve, len(chunk))
                    output.write(chunk)
                digest.update(chunk)
            if proc.wait() != 0:
                raise FinanceStorageSnapshotRetentionError('zstd failed or archive is corrupt')
        except BaseException:
            proc.kill(); proc.wait()
            raise
    proof = {'size_bytes': count, 'sha256': 'sha256:' + digest.hexdigest()}
    if expected is not None and any(proof[k] != expected[k] for k in proof):
        raise FinanceStorageSnapshotRetentionError('decompressed backup differs from original')
    if output is not None:
        output.flush(); os.fsync(output.fileno())
    return proof


def _decode(path, expected, *, output=None, reserve=0):
    _regular(path)
    return _run(['zstd', '-d', '--no-progress', '-c', str(path)], output=output,
                expected=expected, limit=expected['size_bytes'], reserve=reserve, root=path.parent)


def _write_receipt(root, receipt):
    receipt.pop('fingerprint', None)
    receipt['fingerprint'] = _fingerprint(receipt)
    _atomic_write_json(root / RECEIPT, receipt)


def load_receipt(root, manifest, *, require_verified=True):
    path = root / RECEIPT
    _regular(path)
    if path.stat().st_size > 65536:
        raise FinanceStorageSnapshotRetentionError('compression receipt exceeds metadata bound')
    receipt = json.loads(path.read_text())
    if not isinstance(receipt, dict) or not isinstance(receipt.get('files'), dict):
        raise FinanceStorageSnapshotRetentionError('compression receipt shape is invalid')
    if (receipt.get('format') != FORMAT
            or receipt.get('backup_manifest_fingerprint') != manifest['fingerprint']
            or receipt.get('fingerprint') != _fingerprint({k:v for k,v in receipt.items() if k != 'fingerprint'})
            or set(receipt.get('files', {})) - set(DBS)
            or (require_verified and (receipt.get('status') != 'verified' or set(receipt['files']) != set(DBS)))):
        raise FinanceStorageSnapshotRetentionError('compressed backup receipt is invalid')
    logical = {f['name']: f for f in manifest['files']}
    for name, entry in receipt['files'].items():
        if (not isinstance(entry, dict) or type(entry.get('size_bytes')) is not int
                or entry['size_bytes'] <= 0
                or re.fullmatch(r'sha256:[0-9a-f]{64}', str(entry.get('sha256', ''))) is None):
            raise FinanceStorageSnapshotRetentionError('compression member shape is invalid')
        if (entry.get('name') != name + '.zst'
                or entry.get('restored_sha256') != logical[name]['sha256']
                or entry.get('restored_size_bytes') != logical[name]['size_bytes']
                or (require_verified and entry.get('disk_restore_verified') is not True)):
            raise FinanceStorageSnapshotRetentionError('compressed backup member proof differs')
    return receipt


def physical_files(root, manifest, *, hashes=True):
    """Return expected stored inventory; health may request stat-only validation."""
    if not (root / RECEIPT).exists():
        return {f['name']: f for f in manifest['files']}
    receipt = load_receipt(root, manifest)
    result = {f['name']: f for f in manifest['files'] if f['name'] not in DBS}
    for name, entry in receipt['files'].items():
        path = root / entry['name']; _regular(path)
        if path.stat().st_size != entry['size_bytes'] or (hashes and _sha256_file(path) != entry['sha256']):
            raise FinanceStorageSnapshotRetentionError('compressed backup stored bytes drifted')
        result[entry['name']] = entry
    result[RECEIPT] = {'name': RECEIPT, 'size_bytes': (root / RECEIPT).stat().st_size,
                       'sha256': _sha256_file(root / RECEIPT) if hashes else ''}
    return result


def compress_verified_set(root, manifest, *, max_stored_bytes, reserve_bytes, archive_budget, fault_at=''):
    """Idempotent exact partial-set compression. Previous selected set is untouched."""
    require_codec()
    if not root.name.startswith('.') or not root.name.endswith('.partial') or root.is_symlink():
        raise FinanceStorageSnapshotRetentionError('compression requires an unselected partial set')
    logical = {f['name']: f for f in manifest['files']}
    if (root / RECEIPT).exists():
        receipt = load_receipt(root, manifest, require_verified=False)
    else:
        receipt = {'format': FORMAT, 'backup_manifest_fingerprint': manifest['fingerprint'],
                   'status': 'compressing', 'files': {}}
        _write_receipt(root, receipt)
    for name in DBS:
        raw = root / name; packed = root / (name + '.zst'); pending = root / (name + '.zst.pending')
        restored = root / (name + '.restore')
        expected = logical[name]
        entry = receipt['files'].get(name)
        if entry is None:
            _regular(raw)
            if raw.stat().st_size != expected['size_bytes'] or _sha256_file(raw) != expected['sha256']:
                raise FinanceStorageSnapshotRetentionError('compression staging source drifted')
            # Only exact deterministic scratch names, and only with proven original.
            for scratch in (pending, packed):
                if scratch.exists():
                    _regular(scratch); scratch.unlink()
            used = sum(e['size_bytes'] for e in receipt['files'].values())
            with pending.open('xb') as out:
                os.chmod(pending, 0o600)
                proof = _run(['zstd', '-3', '-T1', '--no-progress', '-c', str(raw)], output=out,
                             limit=archive_budget-used, reserve=reserve_bytes, root=root)
            os.replace(pending, packed); _fsync_directory(root)
            entry = {**proof, 'name': packed.name, 'restored_size_bytes': expected['size_bytes'],
                     'restored_sha256': expected['sha256'], 'disk_restore_verified': False}
            receipt['files'][name] = entry; _write_receipt(root, receipt)
        _regular(packed)
        if packed.stat().st_size != entry['size_bytes'] or _sha256_file(packed) != entry['sha256']:
            raise FinanceStorageSnapshotRetentionError('partial compressed bytes drifted')
        if entry['disk_restore_verified'] and not raw.exists() and not restored.exists():
            continue
        # Decode/hash before releasing this unselected staging original. The old
        # selected restore set remains intact throughout this entire operation.
        _decode(packed, expected)
        if raw.exists():
            _regular(raw)
            if _sha256_file(raw) != expected['sha256']:
                raise FinanceStorageSnapshotRetentionError('staging original changed before release')
            raw.unlink(); _fsync_directory(root)
        if fault_at == 'after_compression_staging_release':
            raise RuntimeError('injected fault after compression staging release')
        if restored.exists():
            _regular(restored); restored.unlink()
        _space(root, reserve_bytes, expected['size_bytes'])
        with restored.open('xb') as out:
            os.chmod(restored, 0o600)
            _decode(packed, expected, output=out, reserve=reserve_bytes)
        if _sha256_file(restored) != expected['sha256']:
            raise FinanceStorageSnapshotRetentionError('restored disk readback differs')
        entry['disk_restore_verified'] = True; _write_receipt(root, receipt)
        if fault_at == 'after_compression_disk_verified':
            raise RuntimeError('injected fault after compression disk verification')
        restored.unlink(); _fsync_directory(root)
    receipt['status'] = 'verified'; _write_receipt(root, receipt)
    stored = sum(p.stat().st_size for p in root.iterdir())
    if stored > max_stored_bytes:
        raise FinanceStorageSnapshotRetentionError('compressed retained set exceeds byte cap')
    return receipt


def verify_restored_bytes(root, manifest):
    """Readback without another disk-sized scratch set.

    The publication receipt proves a disk restore. Decoding and matching every
    byte again proves equivalence to those same SQLite/logical checks.
    """
    require_codec()
    physical_files(root, manifest)
    logical = {f['name']: f for f in manifest['files']}
    for name in DBS:
        _decode(root / (name + '.zst'), logical[name])
