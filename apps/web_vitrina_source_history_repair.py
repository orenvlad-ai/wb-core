"""One-submit, held-maintenance publication of observed source history cells."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.production_apply_contract import AmbiguousSubmit
from apps.web_vitrina_web_source_publication import (
    WebSourcePublicationAdapter, validate_observations, digest as request_digest, durable_json,
)
from apps.web_vitrina_history_candidate_build import finished_builder_slot, runtime_storage_admission
from packages.application.business_data_write_barrier import barrier_status
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_source_history_repair import plan_repair, publish_repair, verify_retained

CONTRACT_PATH = Path('artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json')


class WebSourceHistoryRepairAdapter:
    def targets(self, request, operation_id):
        from apps.production_apply_launcher import validate_operation
        if validate_operation(operation_id) != operation_id:
            raise ValueError('source-history-operation-invalid')
        runtime, _ = WebSourcePublicationAdapter().target(request)
        contract_path = runtime.parent/'app'/CONTRACT_PATH
        if Path(request['runtime_contract']).resolve() != contract_path.resolve():
            raise ValueError('source-history-contract-path-invalid')
        raw = contract_path.read_bytes()
        if 'sha256:'+hashlib.sha256(raw).hexdigest() != request['runtime_contract_sha256']:
            raise ValueError('source-history-contract-drift')
        contract = json.loads(raw)
        root = Path(contract['candidate_root'])
        if root != root.resolve() or root.is_relative_to(runtime):
            raise ValueError('source-history-root-invalid')
        source = HistoryStore(root/'history')
        candidate = HistoryStore(root/('source-repair-'+operation_id))
        journal = runtime/'evidence'/(operation_id+'.source-history-before.json')
        return runtime, source, candidate, journal, contract_path, contract

    def authority(self, request, runtime, *, retained=False):
        native = WebSourcePublicationAdapter()
        operation = request['publication_operation_id']
        from apps.production_apply_launcher import validate_operation
        if validate_operation(operation) != operation:
            raise ValueError('source-history-publication-operation-invalid')
        saved = json.loads(native._backup(runtime, operation).read_text())
        published = saved['request']
        # The native request binds each date's scope. A batch may straddle the
        # 94/96 roster boundary, so its union is not a per-day certificate.
        validate_observations(published)
        if (request_digest(published) != request['publication_request_sha256']
                or published.get('phase') != 'publication'
                or published['observations'] != request['observations']
                or published['source_sha256'] != request['source_sha256']
                or published['dates'] != request['dates']
                or published['nm_ids'] != request['nm_ids']
                or published['supplier_identity_sha256'] != request['supplier_identity_sha256']
                or published['runtime_dir'] != request['runtime_dir']
                or published['runtime_sha'] != request['runtime_sha']
                or published['storage_manifest_sha256'] != request['storage_manifest_sha256']
                or (published.get('maintenance_window_id') is not None
                    and published['maintenance_window_id'] != request.get('maintenance_window_id'))):
            raise ValueError('source-history-publication-request-unbound')
        rosters = published['history_roster']
        if (rosters['edition_id'] != request['expected_base_edition']
                or {d:r['nm_ids'] for d,r in rosters['dates'].items()} != request['audited_nm_ids_by_date']):
            raise ValueError('source-history-publication-roster-unbound')
        if native.readback(published, operation, **({'target_only': True} if retained else {}))['state'] != 'applied':
            raise ValueError('source-history-native-publication-unproven')
        return {'publication_operation_id': operation,
            'publication_request_sha256': request['publication_request_sha256'],
            'source_sha256': request['source_sha256'],
            'runtime_sha': request['runtime_sha'], 'storage_manifest_sha256': request['storage_manifest_sha256'],
            'runtime_contract_sha256': request['runtime_contract_sha256'],
            'native_after_images_sha256': request_digest(saved['candidate']['after_images'])}

    def build(self, request, operation_id, *, deadline=None):
        runtime, source, _, _, _, _ = self.targets(request, operation_id)
        authority = self.authority(request, runtime)
        return plan_repair(source, expected_base=request['expected_base_edition'],
            observations=request['observations'], rosters=request['audited_nm_ids_by_date'],
            operation_id=operation_id, authority=authority, deadline=deadline)

    def preview(self, request, operation_id):
        _, source, _, journal, _, _ = self.targets(request, operation_id)
        if journal.exists():
            saved = json.loads(journal.read_text())
            if saved['request_sha256'] != request_digest(request):
                raise ValueError('source-history-operation-request-changed')
            plan = saved['plan']
        else:
            plan = self.build(request, operation_id, deadline=time.monotonic()+180)
        return {'operation_id': operation_id, 'target': str(source.root),
            'scope': {'dates': sorted(plan['patches']), 'changed_cells': plan['changed_cells'],
                'native_proofs_retained': True},
            'prestate_sha256': 'sha256:'+plan['claim']['base_pointer_sha256'],
            'candidate_sha256': request_digest(plan), 'candidate': plan,
            'recovery': {'kind': 'immutable-base-edition-and-exact-object-refs',
                'base_edition_id': request['expected_base_edition'], 'path': str(journal)}}

    @contextmanager
    def admission(self, request, runtime):
        # Hold the actual pause/resume serialization and heavy writer lease,
        # not just a transient JSON observation of the held window.
        self.check_window(request, runtime)
        with WebSourcePublicationAdapter()._write_admission(request, runtime, 'web_source_history_repair'), finished_builder_slot(runtime) as state:
            if state != 'idle':
                raise ValueError('source-history-builder-not-idle')
            yield

    @staticmethod
    def check_window(request, runtime):
        state = barrier_status(runtime)
        if not (state.get('active') is True and state.get('phase') == 'held'
                and state.get('hold_confirmed') is True and state.get('window_kind') == 'maintenance_pause'
                and state.get('window_id') == request.get('maintenance_window_id')
                and request.get('maintenance_window_id')):
            raise ValueError('source-history-maintenance-window-mismatch')

    def apply(self, request, operation_id, preview):
        runtime, source, candidate, journal, contract_path, contract = self.targets(request, operation_id)
        try:
            with self.admission(request, runtime):
                runtime_storage_admission(source.root.parent, contract_path, contract['formula_epoch'])
                if journal.exists():
                    raise ValueError('source-history-already-submitted-readback-only')
                plan = self.build(request, operation_id, deadline=time.monotonic()+180)
                if request_digest(plan) != preview['candidate_sha256']:
                    raise ValueError('source-history-candidate-cas-drift')
                journal.parent.mkdir(mode=0o700, exist_ok=True)
                durable_json(journal, {'request_sha256': request_digest(request), 'plan': plan})
                def revalidate():
                    self.check_window(request, runtime)
                    self.targets(request, operation_id)
                    return self.authority(request, runtime) == plan['claim']['authority']
                publish_repair(source, candidate, plan, revalidate=revalidate, deadline=time.monotonic()+180)
            return {'operation_id': operation_id, 'disposition': 'submitted'}
        except Exception:
            if journal.exists():
                raise AmbiguousSubmit('source-history-requires-readback') from None
            raise

    def readback(self, request, operation_id):
        runtime, source, _, journal, _, _ = self.targets(request, operation_id)
        if not journal.exists():
            return {'operation_id': operation_id, 'state': 'not_submitted'}
        saved = json.loads(journal.read_text())
        if saved['request_sha256'] != request_digest(request):
            raise ValueError('source-history-operation-request-changed')
        plan = saved['plan']; pointer = source._current()
        current = source.edition(pointer['current'])
        matches = all(current.get('source_repair', {}).get(d, {}).get('claim_sha256') == digest(plan['claim'])
                      and current['source_repair'][d]['object_id'] == current['days'].get(d)
                      for d in plan['patches'])
        if not matches:
            return {'operation_id': operation_id, 'state': 'ambiguous', 'reason': 'publication-not-proven'}
        try:
            # Both the original base and exact serving immutable objects must
            # remain readable. Do not invoke apply or rebuild on uncertainty.
            verify_retained(source, plan, current, deadline=time.monotonic()+180)
            if self.authority(request, runtime, retained=True) != plan['claim']['authority']:
                raise ValueError('source-history-native-authority-changed')
            if source._current() != pointer:
                raise ValueError('source-history-readback-pointer-changed')
        except Exception:
            return {'operation_id': operation_id, 'state': 'ambiguous', 'reason': 'readback-proof-failed'}
        return {'operation_id': operation_id, 'state': 'applied',
            'edition_id': pointer['current'], 'dates': sorted(plan['patches'])}
