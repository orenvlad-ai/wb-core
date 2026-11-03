"""Explicit queue retirement of a published contour whose history is unconfirmed.

Only diagnostics changes. This is neither historical completion nor authority to
repeat the original effects. Source bytes, parent intent and book remain intact.
"""
import hashlib
import json
import re
import sqlite3

from apps.production_apply_contract import AmbiguousSubmit

from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_sqlite as sql
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_accounting_historical_stages import require
from packages.application.fbs_accounting_historical_publication import TEMPORARY_CONTRACT, FBO_CONTRACT

ACTION = 'retire_historical_publication_unconfirmed'
CONTRACT = 'historical_publication_retired_unconfirmed_v1'
KEY = 'historical_publication_retirement'
TABLE = 'sheet_vitrina_v1_ready_publications'
MAX_SMALL = 8 * 1024**2
HEADERS = ('operation_id','attempt_id','kind','state','book_version','expected_book','book_operation_id')
FIELDS = dict.fromkeys(('contract','operation_id','attempt_id','manifest_digest','expected_book','after_book','book_operation_id'))
FIELDS['plan'] = dict.fromkeys(('effect_binding','native_plan','fee_scope'))
PARENT_FIELDS = dict.fromkeys(('contract','operation_id','attempt_id','recipe_digest','expected_book',
    'publication_operation_id','publication_attempt_id','authority','unit','native_plan','fee_requests'))


class _Projection:
    """Bounded JSON field selection from a SQLite blob; never expands Ready images.

    Unselected values are scanned as opaque balanced containers/strings. Selected
    fragments alone go through the native JSON decoder. Hash covers every byte.
    """
    tokens = re.compile(rb'["{}\[\]]')
    string_tokens = re.compile(rb'["\\]')

    def __init__(self, blob):
        self.blob, self.buffer, self.offset = blob, b'', 0
        self.hashed = hashlib.sha256()
        self.capture = None
        self.size = 0

    def peek(self):
        if self.offset == len(self.buffer):
            self.buffer = self.blob.read(64 * 1024); self.offset = 0
            self.hashed.update(self.buffer)
        return self.buffer[self.offset:self.offset + 1]

    def take(self, count=1):
        value = self.buffer[self.offset:self.offset + count]; self.offset += count
        if self.capture is not None:
            self.size += len(value)
            require(self.size <= MAX_SMALL, 'retirement_projection_size_limit')
            self.capture.append(value)
        return value

    def whitespace(self):
        while self.peek() in (b' ', b'\n', b'\r', b'\t'): self.take()

    def string(self):
        require(self.peek() == b'"', 'retirement_source_json_invalid'); self.take()
        while self.peek():
            token = self.string_tokens.search(self.buffer, self.offset)
            if token is None:
                self.take(len(self.buffer) - self.offset); continue
            self.take(token.start() - self.offset)
            if self.take() == b'"': return
            require(bool(self.peek()), 'retirement_source_json_invalid'); self.take()
        require(False, 'retirement_source_json_invalid')

    def skip(self):
        self.whitespace(); first = self.peek()
        if first == b'"': self.string(); return
        if first in (b'{', b'['):
            stack = [b'}' if self.take() == b'{' else b']']
            while stack:
                require(bool(self.peek()), 'retirement_source_json_invalid')
                token = self.tokens.search(self.buffer, self.offset)
                if token is None:
                    self.take(len(self.buffer) - self.offset); continue
                self.take(token.start() - self.offset); char = self.peek()
                if char == b'"': self.string()
                elif char in (b'{', b'['): stack.append(b'}' if self.take() == b'{' else b']')
                else: require(self.take() == stack.pop(), 'retirement_source_json_invalid')
        else:
            require(bool(first), 'retirement_source_json_invalid')
            while self.peek() and self.peek() not in (b',', b'}', b']', b' ', b'\n', b'\t', b'\r'): self.take()

    def fragment(self, reader):
        self.capture = []; self.size = 0
        reader(); value = b''.join(self.capture); self.capture = None
        return json.loads(value)

    def object(self, fields):
        self.whitespace(); require(self.peek() == b'{', 'retirement_source_json_invalid'); self.take()
        result = {}; seen = set(); self.whitespace()
        while self.peek() != b'}':
            name = self.fragment(self.string)
            require(name not in seen, 'retirement_source_duplicate_key'); seen.add(name)
            self.whitespace(); require(self.peek() == b':', 'retirement_source_json_invalid'); self.take(); self.whitespace()
            if name in fields:
                result[name] = self.object(fields[name]) if fields[name] is not None else self.fragment(self.skip)
            else: self.skip()
            self.whitespace()
            if self.peek() == b'}': break
            require(self.peek() == b',', 'retirement_source_json_invalid'); self.take(); self.whitespace()
        self.take(); self.whitespace()
        return result


def _source(conn, operation_id, attempt_id, fields):
    row = conn.execute(f'SELECT rowid,{",".join(HEADERS)},diagnostics_json FROM {TABLE} WHERE operation_id=? AND attempt_id=?',
        (operation_id, attempt_id)).fetchone()
    require(row is not None, 'retirement_publication_missing')
    header = {key: row[key] for key in HEADERS}
    require(len(row['diagnostics_json'].encode()) <= MAX_SMALL, 'retirement_diagnostics_size_limit')
    with conn.blobopen(TABLE, 'inputs_json', row['rowid'], readonly=True) as blob:
        require(len(blob) <= 1024**3, 'retirement_source_size_limit')
        reader = _Projection(blob); projection = reader.object(fields)
        require(not reader.peek(), 'retirement_source_json_invalid')
        header.update(inputs_sha256='sha256:' + reader.hashed.hexdigest(), inputs_bytes=len(blob))
    return header, projection, row['diagnostics_json']


def _snapshot(conn, request, *, check_native):
    child, manifest, raw = _source(conn, request['publication_operation_id'], request['publication_attempt_id'], FIELDS)
    require(child['kind'] in (TEMPORARY_CONTRACT, FBO_CONTRACT) and child['state'] == 'complete', 'retirement_complete_typed_child_required')
    require(manifest['contract'] == child['kind'] and all(manifest[key] == child[key] for key in ('operation_id','attempt_id','expected_book','book_operation_id'))
        and manifest['after_book'] == child['book_version'], 'retirement_child_binding_changed')
    binding = manifest['plan']['effect_binding']
    parent, recipe, parent_raw = _source(conn, binding['operation_id'], binding['attempt_id'], PARENT_FIELDS)
    expected_kind = 'fbs_temporary_allocation_effect_intent_v1' if child['kind'] == TEMPORARY_CONTRACT else 'fbs_historical_fbo_effect_intent_v1'
    require(parent['kind'] == recipe['contract'] == expected_kind and parent['state'] == 'prepared', 'retirement_prepared_parent_required')
    require(all(recipe[key] == parent[key] == binding[key] for key in ('operation_id','attempt_id'))
        and recipe['recipe_digest'] == binding['recipe_digest'] and recipe['expected_book'] == parent['expected_book'] == child['expected_book']
        and recipe['publication_operation_id'] == child['operation_id']
        and recipe.get('publication_attempt_id', recipe['attempt_id']) == child['attempt_id'], 'retirement_source_binding_changed')
    ready.check_pinned_authority(conn, recipe['authority'])
    diagnostics = json.loads(raw)
    require(not any(diagnostics.get(key) for key in ('temporary_allocation_scope_complete','historical_fbo_scope_complete','historical_operator_complete','historical_history_ack')),
        'retirement_confirmed_scope_forbidden')
    require(child['inputs_sha256'] == request['publication_inputs_sha256'] and parent['inputs_sha256'] == request['parent_inputs_sha256']
        and manifest['manifest_digest'] == request['publication_manifest_digest'] and binding['recipe_digest'] == request['recipe_digest'],
        'retirement_expected_source_changed')
    if check_native:
        if child['kind'] == TEMPORARY_CONTRACT:
            from packages.application.fbs_temporary_allocation_action import _verify_unit
            require(_verify_unit(conn, recipe['unit']), 'retirement_native_source_changed')
        else:
            from packages.application.fbs_historical_fbo_publication import capture_fee_scope
            from packages.application.fbs_historical_fbo_native import _verify_existing
            plan = manifest['plan']
            require(plan['native_plan'] == recipe['native_plan'] and _verify_existing(conn, recipe['native_plan'])
                and capture_fee_scope(conn, native_plan=recipe['native_plan'], fee_requests=recipe['fee_requests']) == plan['fee_scope'],
                'retirement_native_source_changed')
    return dict(child=child, parent=parent, parent_diagnostics_sha256=ready.digest(parent_raw),
        binding=binding, manifest_digest=manifest['manifest_digest']), raw


def retirement_status(conn, operation_id, attempt_id):
    """Fail closed: malformed or changed retirement never exempts a queue item."""
    row = conn.execute(f'SELECT diagnostics_json FROM {TABLE} WHERE operation_id=? AND attempt_id=?', (operation_id, attempt_id)).fetchone()
    if not row: return None
    proof = json.loads(row[0]).get(KEY)
    if not proof: return None
    try:
        require(proof['contract'] == CONTRACT and proof['status'] == 'retired_unconfirmed' and proof['history_confirmed'] is False
            and proof['proof_sha256'] == fingerprint({k:v for k,v in proof.items() if k != 'proof_sha256'}), 'retirement_proof_invalid')
        require(proof['source_request']['publication_operation_id'] == operation_id
            and proof['source_request']['publication_attempt_id'] == attempt_id, 'retirement_proof_wrong_publication')
        current, raw = _snapshot(conn, proof['source_request'], check_native=False)
        before = json.loads(raw); before.pop(KEY, None)
        require(current == proof['source'] and ready.digest(canonical(before)) == proof['original_diagnostics_sha256'], 'retirement_proof_changed')
        return proof
    except (ValueError, KeyError, TypeError, sqlite3.Error): return None


class HistoricalPublicationRetirementAction:
    def preview(self, request, operation_id, *, root):
        from packages.application import fbs_historical_fbo_action as shared
        from packages.application.storage_registry import StoreRegistry
        require(isinstance(request.get('approval_reference'), str) and 0 < len(request['approval_reference']) <= 2000, 'retirement_approval_reference_required')
        with shared._held(root, request) as held, ready.readonly(StoreRegistry(root).resolve('operational')) as conn:
            source, raw = _snapshot(conn, request, check_native=True)
            require(KEY not in json.loads(raw), 'retirement_already_decided_readback_only')
        source_request = {key: request[key] for key in ('publication_operation_id','publication_attempt_id','publication_inputs_sha256','parent_inputs_sha256','publication_manifest_digest','recipe_digest')}
        proof = dict(contract=CONTRACT, operation_id=operation_id, status='retired_unconfirmed', history_confirmed=False,
            source=source, source_request=source_request, original_diagnostics_sha256=ready.digest(canonical(json.loads(raw))),
            approval_reference=request['approval_reference'], maintenance=held)
        proof['proof_sha256'] = fingerprint(proof)
        return dict(operation_id=operation_id, target=str(root), scope=dict(action=ACTION, publication=source['child'], history_status='retired_unconfirmed'),
            prestate_sha256=fingerprint(dict(source=source, diagnostics=raw, held=held)), candidate_sha256=fingerprint(proof),
            recovery=dict(kind='diagnostics_only_backup', original_diagnostics=str(shared._directory(root, operation_id)/'retirement-original-diagnostics.json')),
            prepared=proof)

    def apply(self, request, operation_id, preview, *, root):
        from packages.application import fbs_historical_fbo_action as shared
        from packages.application.storage_registry import StoreRegistry
        fresh = self.preview(request, operation_id, root=root)
        require(all(fresh[key] == preview[key] for key in ('prestate_sha256','candidate_sha256')), 'retirement_compare_failed')
        db = StoreRegistry(root).resolve('operational')
        submitted = False
        try:
            with shared._held(root, request) as held, sql.connection(db, phase_name='retirement_connection') as conn:
                conn.row_factory = sqlite3.Row
                with sql.transaction(conn, db_path=db, phase_name='retirement'):
                    source, raw = _snapshot(conn, request, check_native=True)
                    require(KEY not in json.loads(raw) and fingerprint(dict(source=source, diagnostics=raw, held=held)) == preview['prestate_sha256'], 'retirement_compare_failed')
                    backup = shared._directory(root, operation_id)/'retirement-original-diagnostics.json'
                    require(not backup.exists(), 'retirement_existing_backup_readback_required')
                    submitted = True
                    shared._private(backup, dict(operation_id=operation_id, source=source, diagnostics_json=raw))
                    diagnostics = json.loads(raw); diagnostics[KEY] = fresh['prepared']
                    require(conn.execute(f'UPDATE {TABLE} SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND kind=? AND state=\'complete\' AND diagnostics_json=?',
                        (canonical(diagnostics), request['publication_operation_id'], request['publication_attempt_id'], source['child']['kind'], raw)).rowcount == 1, 'retirement_cas_failed')
        except Exception:
            if submitted: raise AmbiguousSubmit('retirement_same_operation_readback_required') from None
            raise
        return dict(operation_id=operation_id, disposition='submitted')

    def readback(self, request, operation_id, *, root):
        from packages.application.storage_registry import StoreRegistry
        from packages.application import fbs_historical_fbo_action as shared
        with ready.readonly(StoreRegistry(root).resolve('operational')) as conn:
            row = conn.execute(f'SELECT diagnostics_json FROM {TABLE} WHERE operation_id=? AND attempt_id=?', (request['publication_operation_id'], request['publication_attempt_id'])).fetchone()
            if not row or KEY not in json.loads(row[0]):
                uncertain = (shared._directory(root, operation_id)/'retirement-original-diagnostics.json').exists()
                return dict(operation_id=operation_id, state='ambiguous' if uncertain else 'not_submitted')
            proof = retirement_status(conn, request['publication_operation_id'], request['publication_attempt_id'])
            good = proof and proof['operation_id'] == operation_id and proof['approval_reference'] == request['approval_reference']
            good = good and all(proof['source_request'][key] == request[key] for key in proof['source_request'])
            return dict(operation_id=operation_id, state='applied' if good else 'ambiguous', history_status='retired_unconfirmed' if good else 'unconfirmed',
                history_confirmed=False, scope_complete=False, parent_state='prepared', next_step='none' if good else 'same_operation_readback_only')
