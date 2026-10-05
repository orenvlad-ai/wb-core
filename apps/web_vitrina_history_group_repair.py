"""Explicit bounded maintenance operation for saved GROUP cells only."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_history_candidate_build import (
    history_procedure_admission, runtime_storage_admission, admission,
    finished_builder_slot, candidate_singleflight, bounded_worker,
    RegistryUploadDbBackedRuntime, StoreRegistry, MaintenanceAdmissionBlocked,
)
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable, _atomic, _read
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_group_repair import (
    _catalog, _unit, _deadline, build_group_repair_candidate,
    preview_group_repair, publish_group_repair,
)
from packages.application.web_vitrina_group_repair_inputs import (
    prepare_group_repair_day, GroupRepairTransform, captured_repair_cost_bindings,
    load_repair_cost_basis,
)
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
from packages.application.web_vitrina_window_read_context import window_read_context


def code_proof(contract):
    hashes = contract.get('repair_code_hashes', {})
    required = {'apps/web_vitrina_history_group_repair.py',
        'packages/application/web_vitrina_history_group_repair.py',
        'packages/application/web_vitrina_group_repair_inputs.py',
        'packages/application/web_vitrina_group_blocks.py'}
    if set(hashes) != required:
        raise ValueError('group_repair_code_contract_missing')
    repo = Path(__file__).resolve().parents[1]
    for name, expected in hashes.items():
        if hashlib.sha256((repo / name).read_bytes()).hexdigest() != expected:
            raise ValueError('group_repair_code_drift')
    return digest(hashes)


def warm_cache(previous, cache, candidate, deadline):
    marker = cache/'source-proofs.json'
    if marker.exists():
        return
    paths = sorted(previous.glob('*.rows.json')) + [previous/'source-proofs.json']
    copied = 0
    for path in paths:
        _deadline(deadline)
        if path.is_symlink() or not path.is_file():
            raise ValueError('group_repair_cache_path_invalid')
        copied += path.stat().st_size
        if copied > 128 * 1024**2:
            raise HistoryUnavailable('source_cache_resource_limit')
        candidate._reserve(path.stat().st_size)
        temporary = cache/('.proof-warming-'+uuid.uuid4().hex)
        try:
            with path.open('rb') as source, temporary.open('xb') as target:
                os.chmod(temporary,0o600)
                while True:
                    _deadline(deadline)
                    data=source.read(1024*1024)
                    if not data:
                        break
                    target.write(data)
                target.flush();os.fsync(target.fileno())
            _deadline(deadline)
            os.replace(temporary,cache/path.name)
            from packages.application.web_vitrina_history_store import _directory_fd
            with _directory_fd(cache) as descriptor:
                os.fsync(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def prepare_inputs(args, contract, source, candidate, deadline):
    """Keep immutable prepared patches; metadata capture never invokes compiler."""
    old = source.edition(args.expected_base)
    code = code_proof(contract)
    directory = candidate.root / 'repair-inputs'
    index_path = directory / 'index.json'
    with candidate._writer():
        if (source._current() or {}).get('current') != args.expected_base:
            raise HistoryUnavailable('history_group_repair_superseded')
        directory.mkdir(mode=0o700, exist_ok=True)
        index = _read(index_path) if index_path.exists() else {
            'base': args.expected_base, 'code': code, 'days': {}, 'total': len(old['days'])}
        if index['base'] != args.expected_base or index['code'] != code:
            raise HistoryUnavailable('history_group_repair_resume_conflict')
        pending = sorted(set(old['days']) - set(index['days']))[:args.max_days]
        if not pending:
            return {'status': 'inputs_ready', 'completed': len(index['days']), 'total': index['total']}
        # The old private cache only accelerates native proof reads. Equality
        # with the published epoch/token, not cache existence, grants authority.
        cache = candidate.root / 'native-proofs'
        cache.mkdir(mode=0o700, exist_ok=True)
        previous = Path(contract['group_migration_root']) / 'proofs'
        warm_cache(previous,cache,candidate,deadline)
        old_cache = _read(cache / 'source-proofs.json')
        old_formula = old_cache['source'][3]
        runtime = RegistryUploadDbBackedRuntime(args.runtime_dir, store_registry=StoreRegistry(args.runtime_dir))
        adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=args.runtime_dir,
            cache_dir=cache, now=datetime.now(timezone.utc), date_from=min(old['days']),
            date_to=max(old['days']), formula_epoch=old_formula,
            max_capture_seconds=min(20, max(0.1, deadline-time.monotonic())))
        processed = 0
        catalogs = {}
        with window_read_context(runtime.db_path, runtime_dir=args.runtime_dir):
            vector = adapter.capture()
            bindings = captured_repair_cost_bindings(adapter)
            for day in pending:
                _deadline(deadline)
                catalog_id = source.day_catalogs(old)[day]
                catalog = _catalog(source, catalog_id, deadline, catalogs)
                object_id = old['days'][day]
                unit, _, _ = _unit(source, source.root/'objects'/(object_id+'.sqlite3'),
                                  object_id, day, catalog, deadline)
                proof = source.day_proofs(old)[day]
                authority = {'old_epoch': proof['epoch'], 'fresh_epoch': vector['epoch'],
                    'old_token': proof['token'], 'fresh_token': vector['dates'][day]}
                basis, receipt = load_repair_cost_basis(runtime, day, catalog, unit['cells'],
                    accepted_binding=bindings.get(day), authority=authority, deadline=deadline)
                prepared = prepare_group_repair_day(day, catalog, unit['cells'], cost_basis=basis)
                prepared['cost_authority_receipt'] = receipt
                prepared['auxiliary_digests']['accepted_cost_authority'] = digest(receipt)
                _deadline(deadline)
                if (source._current() or {}).get('current') != args.expected_base:
                    raise HistoryUnavailable('history_group_repair_superseded')
                candidate._reserve(len(json.dumps(prepared, ensure_ascii=False).encode())+4096)
                _atomic(directory/(day+'.json'), prepared)
                index['days'][day] = digest(prepared)
                _atomic(index_path, index)
                processed += 1
        return {'status': 'inputs_ready' if len(index['days']) == index['total'] else 'inputs_pending',
            'completed': len(index['days']), 'total': index['total'], 'processed': processed,
            'source_reads': adapter.stats}


def prepared_plan(args, contract, source, candidate, deadline):
    index = _read(candidate.root/'repair-inputs/index.json')
    old = source.edition(args.expected_base)
    code = code_proof(contract)
    if (index['base'] != args.expected_base or index['code'] != code
            or set(index['days']) != set(old['days'])):
        raise HistoryUnavailable('history_group_repair_inputs_incomplete')
    allowed, auxiliary = {}, {}
    def get(day):
        _deadline(deadline)
        prepared = _read(candidate.root/'repair-inputs'/(day+'.json'))
        if digest(prepared) != index['days'][day]:
            raise HistoryUnavailable('history_group_repair_input_changed')
        return prepared
    for day in sorted(index['days']):
        prepared = get(day)
        if prepared['patch']:
            allowed[day] = sorted(prepared['patch'])
            auxiliary[day] = prepared['auxiliary_digests']
    def transform(day, catalog, cells):
        return GroupRepairTransform({day:get(day)})(day,catalog,cells)
    return allowed, {'transform_code_hash':code,'auxiliary_digests':auxiliary}, transform


def worker(args, contract, root, deadline):
    source = HistoryStore(Path(contract['candidate_root'])/'history')
    candidate = HistoryStore(root/'group-repair-history')
    if args.action == 'publish' and (source._current() or {}).get('current') == args.expected_candidate:
        return publish_group_repair(source,candidate,expected_base=args.expected_base,
            expected_candidate=args.expected_candidate,preview_token=args.preview_token,
            revalidate=lambda: {},deadline_monotonic=deadline)
    if args.action == 'prepare':
        return prepare_inputs(args,contract,source,candidate,deadline)
    allowed, metadata, transform = prepared_plan(args,contract,source,candidate,deadline)
    if args.action == 'build':
        return build_group_repair_candidate(source,candidate,expected_base=args.expected_base,
            allowlisted_group_row_ids=allowed,metadata=metadata,transform_day=transform,
            max_days_per_call=args.max_days,deadline_monotonic=deadline)
    preview = preview_group_repair(source,candidate,expected_base=args.expected_base,
        expected_candidate=args.expected_candidate,deadline_monotonic=deadline)
    if args.action == 'preview':
        return preview
    def revalidate():
        fresh_allowed, fresh_metadata, _ = prepared_plan(args,contract,source,candidate,deadline)
        claim = preview['repair_claim']
        if (fresh_allowed != claim['allowlisted_group_row_ids']
                or any(fresh_metadata[k] != claim[k] for k in fresh_metadata)):
            return {}
        return claim
    return publish_group_repair(source,candidate,expected_base=args.expected_base,
        expected_candidate=args.expected_candidate,preview_token=args.preview_token,
        revalidate=revalidate,deadline_monotonic=deadline)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir',type=Path,required=True)
    parser.add_argument('--runtime-contract',type=Path,required=True)
    parser.add_argument('--expected-base',required=True)
    parser.add_argument('--action',choices=('prepare','build','preview','publish'),required=True)
    parser.add_argument('--expected-candidate',default='')
    parser.add_argument('--preview-token',default='')
    parser.add_argument('--maintenance-window-id',required=True)
    parser.add_argument('--manual',action='store_true')
    parser.add_argument('--budget-seconds',type=float,default=180)
    parser.add_argument('--max-days',type=int,default=5)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not args.manual or not args.maintenance_window_id or not 0<args.budget_seconds<=180 or not 1<=args.max_days<=31:
        raise ValueError('explicit_held_bounded_group_repair_required')
    if args.action in {'preview','publish'} and not args.expected_candidate:
        raise ValueError('exact_candidate_required')
    if args.action=='publish' and not args.preview_token:
        raise ValueError('exact_preview_token_required')
    try:
        with history_procedure_admission(args):
            contract=_read(args.runtime_contract)
            root=Path(contract['group_migration_root'])
            runtime_storage_admission(root,args.runtime_contract,contract['formula_epoch'],group_migration=True)
            code_proof(contract)
            if args.worker:
                result=worker(args,contract,root,time.monotonic()+args.budget_seconds)
            else:
                state=admission(args.runtime_dir)
                if state!='idle':
                    result={'status':'skipped_'+state,'last_good_retained':True}
                else:
                    with finished_builder_slot(args.runtime_dir) as slot:
                        if slot!='idle':
                            result={'status':'skipped_'+slot,'last_good_retained':True}
                        else:
                            with candidate_singleflight(root) as acquired:
                                result=bounded_worker([sys.executable,str(Path(__file__).resolve()),
                                    *sys.argv[1:],'--worker'],args.budget_seconds) if acquired else {
                                    'status':'skipped_busy','last_good_retained':True}
    except MaintenanceAdmissionBlocked as exc:
        result={'status':'skipped_maintenance','reason':str(exc),'last_good_retained':True}
    print(json.dumps(result))
    return 1 if result.get('status')=='build_failed' else 0


if __name__=='__main__':
    raise SystemExit(main())
