"""Private bounded dated-object proofs and actual-supervisor closed-date ack.

These proofs are derived observations, never business admission or retry tokens.
Only the fixed authenticated history child reads cells; the live parent consumes
its own single-use proof object while all four ownership descriptors remain held.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import MappingProxyType
import zlib

from packages.application.owned_history_worker_capability import HistoryDelegationError, fingerprint, read_json


def closed_receipt_digest(value):
    from packages.application.ready_publication import canonical, digest
    return digest(canonical(value))


def source_stamp(runtime_dir, db_path):
    """Bounded physical fence around the consumed native read, never freshness."""
    from packages.application.storage_registry import MANIFEST_FILENAME
    runtime_dir = Path(runtime_dir).resolve()
    databases = (Path(db_path).resolve(), runtime_dir / 'fbs-snapshot-accounting.sqlite3')
    paths = [Path(str(path) + suffix) for path in databases for suffix in ('', '-wal')]
    paths += [runtime_dir / MANIFEST_FILENAME, runtime_dir / '.auto-updates-policy.json',
              runtime_dir / '.web-vitrina-fbs-lifecycle-last-good.json']
    result = {}
    for path in paths:
        try:
            value = path.stat()
        except FileNotFoundError:
            result[str(path)] = None
        else:
            if path.is_symlink() or not stat.S_ISREG(value.st_mode):
                raise HistoryDelegationError('history_source_stamp_unsafe')
            result[str(path)] = [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]
    return result

def _object(store, day, ref, catalog_id, deadline):
    from packages.application.web_vitrina_history_compiler import digest
    if any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None for value in (ref, catalog_id)):
        raise HistoryDelegationError('history_dated_object_identity_invalid')
    path = store.root / 'objects' / (ref + '.sqlite3')
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise HistoryDelegationError('history_dated_object_unsafe')
    catalog = read_json(store.root / 'catalogs' / (catalog_id + '.json'))
    if digest(catalog) != catalog_id:
        raise HistoryDelegationError('history_dated_catalog_corrupt')
    with closing(store._open_day(path)) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        metadata = conn.execute('SELECT payload FROM metadata LIMIT 2').fetchall()
        if len(metadata) != 1 or len(metadata[0][0].encode()) > 16 * 1024**2:
            raise HistoryDelegationError('history_dated_metadata_invalid')
        unit = json.loads(metadata[0][0]); cells = {}; size = 0
        for row_id, encoded in conn.execute('SELECT row_id,payload FROM cells ORDER BY row_id'):
            if time.monotonic() >= deadline:
                raise HistoryDelegationError('history_dated_readback_deadline')
            decoder = zlib.decompressobj(); raw = decoder.decompress(encoded, 65537); size += len(raw)
            if len(raw) > 65536 or size > 64 * 1024**2 or not decoder.eof or decoder.unconsumed_tail:
                raise HistoryDelegationError('history_dated_cell_limit')
            cell = json.loads(raw)
            if not isinstance(cell, list) or len(cell) != 16 or row_id in cells:
                raise HistoryDelegationError('history_dated_cell_corrupt')
            cells[row_id] = cell
    unit['cells'] = cells
    if (unit['date'] != day or unit['context_epoch'] != catalog['context_epoch']
            or set(cells) != set(catalog['rows']) or digest(unit) != ref):
        raise HistoryDelegationError('history_dated_object_corrupt')


def verified_derived_state(store, anchor, target, deadline, *, first_baseline=False):
    """Exact completed-day set, never cache size/mtime or a mere file presence."""
    from packages.application.web_vitrina_history_compiler import digest
    vector, scope = anchor['vector'], anchor['scope_dates']
    pointer = store._current(); current_id = pointer['current'] if pointer else None
    old = store.edition() if pointer else None
    completed = {}
    if old:
        proofs = store.day_proofs(old); catalogs = store.day_catalogs(old)
        for day in scope:
            if proofs.get(day) == {'epoch': vector['epoch'], 'token': vector['dates'][day]}:
                _object(store, day, old['days'][day], catalogs[day], deadline)
                completed[day] = old['days'][day]
    status = store.rolling_status(vector, anchor['window_to'], anchor['backfill_dates'])
    rolling = {key: value for key, value in status.items() if key != 'dirty_dates'}
    # Canonical status-only publication preserves every immutable day/catalog
    # and all other edition fields; it needs no compiler intent or resend.
    metadata_only = False
    if current_id != anchor['base'] and anchor['base'] is not None:
        base = store.edition(anchor['base'])
        metadata_only = ({key: value for key, value in old.items() if key != 'rolling14'} ==
                         {key: value for key, value in base.items() if key != 'rolling14'}
                         and old.get('rolling14') == rolling)
    # A changed CURRENT must be this exact target, not a newly adopted base.
    if current_id != anchor['base']:
        if (pointer.get('previous') != anchor['base'] or set(completed) != set(scope)
                or not (metadata_only or target and old['catalog'] == target['merged_catalog'])):
            raise HistoryDelegationError('history_readback_current_superseded')
    pending_path = store.root / 'PENDING.json'
    pending = read_json(pending_path) if pending_path.exists() else None
    if current_id == anchor['base'] and target is not None and pending:
        exact = (pending.get('target') == target['target'] and pending.get('base') == anchor['base']
            and pending.get('vector') == vector and pending.get('catalog') == target['merged_catalog']
            and target['target'] == digest([vector, target['merged_catalog'], sorted(scope)]))
        if not exact:
            if not first_baseline:
                raise HistoryDelegationError('history_readback_pending_target_changed')
            # Only a new independently admitted supervisor's first pre-write
            # baseline may inspect an older canonical checkpoint. Its target
            # never authorizes retry and reused days never count as new progress.
            old_vector = pending.get('vector')
            if (pending.get('rolling14') is not True or pending.get('base') != anchor['base']
                    or not isinstance(old_vector, dict) or not isinstance(old_vector.get('dates'), dict)
                    or not isinstance(old_vector.get('epoch'), str)
                    or not isinstance(pending.get('day_proofs'), dict)
                    or not isinstance(pending.get('day_catalogs'), dict) or not isinstance(pending.get('refs'), dict)
                    or len(pending['refs']) > 366 or len(old_vector['dates']) > 366
                    or any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None
                           for value in [pending.get('target'), pending.get('catalog'), *pending['refs'].values(), *pending['day_catalogs'].values()])):
                raise HistoryDelegationError('history_stale_pending_metadata_invalid')
            try:
                keys = set(old_vector['dates']) | set(pending['refs']) | set(pending['day_proofs']) | set(pending['day_catalogs'])
                if (len(keys) > 366 or any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in keys)
                        or any(not isinstance(proof, dict) or set(proof) != {'epoch', 'token'}
                            or not isinstance(proof['epoch'], str) or not isinstance(proof['token'], str)
                            for proof in pending['day_proofs'].values())):
                    raise ValueError('invalid dated metadata')
            except (TypeError, ValueError):
                raise HistoryDelegationError('history_stale_pending_metadata_invalid') from None
            old_catalog = read_json(store.root / 'catalogs' / (pending['catalog'] + '.json'))
            if digest(old_catalog) != pending['catalog']:
                raise HistoryDelegationError('history_stale_pending_catalog_corrupt')
        for day in scope:
            if (pending.get('day_proofs', {}).get(day) == {'epoch': vector['epoch'], 'token': vector['dates'][day]}
                    and pending.get('day_catalogs', {}).get(day) == target['fresh_catalog']):
                if not exact and pending['day_proofs'][day] != {
                        'epoch': pending['vector']['epoch'], 'token': pending['vector']['dates'].get(day)}:
                    raise HistoryDelegationError('history_stale_pending_dated_proof_invalid')
                ref = pending['refs'][day]
                _object(store, day, ref, target['fresh_catalog'], deadline)
                completed[day] = ref
    terminal = bool(old and set(completed) == set(scope) and all(
        store.day_proofs(old).get(day) == {'epoch': vector['epoch'], 'token': vector['dates'][day]} for day in scope)
        and not status['dirty_dates'] and old.get('rolling14') == rolling)
    return {'current': pointer, 'completed': completed, 'terminal': terminal}


@dataclass(frozen=True)
class _VerifiedClosedHistory:
    supervisor: object
    invocation: str
    receipt_digest: str
    backfill_dates: tuple[str, ...]
    current: dict
    native: dict
    source_stamp: dict


def consume_closed_history_ack(proof, runtime_dir, receipt_digest, backfill_dates):
    """Only a live supervisor's own actual terminal result is consumable once."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    if type(proof) is not _VerifiedClosedHistory:
        raise HistoryDelegationError('history_closed_ack_not_supervised')
    owner = proof.supervisor
    owner._check()
    if (owner.pid != os.getpid() or owner.thread is not threading.current_thread() or owner._process is not None
            or owner._closed_proof is not proof or owner.runtime.runtime_dir.resolve() != Path(runtime_dir).resolve()
            or require_heavy_owner(runtime_dir).operation != 'cycle' or proof.receipt_digest != receipt_digest
            or proof.backfill_dates != tuple(backfill_dates)
            or source_stamp(runtime_dir, owner.runtime.db_path) != proof.source_stamp
            or read_json(Path(owner.config.candidate_root) / 'history' / 'CURRENT.json', limit=4096) != proof.current):
        raise HistoryDelegationError('history_closed_ack_owner_or_target_changed')
    owner._closed_proof = None
    return MappingProxyType({day: MappingProxyType({'edition_id': proof.current['current'], **deepcopy(proof.native[day])})
        for day in proof.backfill_dates})


@dataclass(frozen=True)
class _VerifiedHistoricalHistory:
    supervisor: object
    invocation: str
    receipt_digest: str
    dates: tuple[str, ...]
    current: dict
    native: dict
    source_stamp: dict


def consume_historical_history_ack(proof, runtime_dir, receipt_digest, dates):
    """Separate one-use actual-supervisor authority; never ClosedBacklog."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    if type(proof) is not _VerifiedHistoricalHistory:
        raise HistoryDelegationError('historical_history_ack_not_supervised')
    owner=proof.supervisor
    owner._check()
    if (owner.pid!=os.getpid() or owner.thread is not threading.current_thread() or owner._process is not None
            or owner._historical_proof is not proof or owner.runtime.runtime_dir.resolve()!=Path(runtime_dir).resolve()
            or require_heavy_owner(runtime_dir).operation!='cycle' or proof.receipt_digest!=receipt_digest
            or proof.dates!=tuple(dates) or source_stamp(runtime_dir,owner.runtime.db_path)!=proof.source_stamp
            or read_json(Path(owner.config.candidate_root)/'history'/'CURRENT.json',limit=4096)!=proof.current):
        raise HistoryDelegationError('historical_history_ack_owner_or_target_changed')
    owner._historical_proof=None
    return MappingProxyType({day:MappingProxyType(deepcopy(proof.native[day])) for day in proof.dates})






@dataclass(frozen=True)
class _VerifiedSupplierHistory:
    supervisor: object
    invocation: str
    receipt_digest: str
    dates: tuple[str, ...]
    current: dict
    native: dict
    source_stamp: dict


def consume_supplier_history_ack(proof, runtime_dir, receipt_digest, dates):
    """Separate one-use supplier cost authority from the authenticated live parent."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    if type(proof) is not _VerifiedSupplierHistory:
        raise HistoryDelegationError('supplier_history_ack_not_supervised')
    owner=proof.supervisor;owner._check()
    if (owner.pid!=os.getpid() or owner.thread is not threading.current_thread() or owner._process is not None
            or owner._supplier_proof is not proof or owner.runtime.runtime_dir.resolve()!=Path(runtime_dir).resolve()
            or require_heavy_owner(runtime_dir).operation!='cycle' or proof.receipt_digest!=receipt_digest
            or proof.dates!=tuple(dates) or source_stamp(runtime_dir,owner.runtime.db_path)!=proof.source_stamp
            or read_json(Path(owner.config.candidate_root)/'history'/'CURRENT.json',limit=4096)!=proof.current):
        raise HistoryDelegationError('supplier_history_ack_owner_or_target_changed')
    owner._supplier_proof=None
    return MappingProxyType({day:MappingProxyType(deepcopy(proof.native[day])) for day in proof.dates})


@dataclass(frozen=True)
class _VerifiedPolicyHistory:
    supervisor: object
    invocation: str
    receipt_digest: str
    dates: tuple[str, ...]
    current: dict
    native: dict
    source_stamp: dict


def consume_policy_history_ack(proof, runtime_dir, receipt_digest, dates):
    """Separate one-use policy authority from the authenticated live parent."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    if type(proof) is not _VerifiedPolicyHistory:
        raise HistoryDelegationError('policy_history_ack_not_supervised')
    owner=proof.supervisor;owner._check()
    if (owner.pid!=os.getpid() or owner.thread is not threading.current_thread() or owner._process is not None
            or owner._policy_proof is not proof or owner.runtime.runtime_dir.resolve()!=Path(runtime_dir).resolve()
            or require_heavy_owner(runtime_dir).operation!='cycle' or proof.receipt_digest!=receipt_digest
            or proof.dates!=tuple(dates) or source_stamp(runtime_dir,owner.runtime.db_path)!=proof.source_stamp
            or read_json(Path(owner.config.candidate_root)/'history'/'CURRENT.json',limit=4096)!=proof.current):
        raise HistoryDelegationError('policy_history_ack_owner_or_target_changed')
    owner._policy_proof=None
    return MappingProxyType({day:MappingProxyType(deepcopy(proof.native[day])) for day in proof.dates})
