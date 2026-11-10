"""Bounded ProductionApply import of reviewed legacy FBO source evidence.

Only two append-only evidence tables are written. Native close receipts,
revisions, pools and historical accounting are never rewritten or replayed.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from apps.production_apply_contract import AmbiguousSubmit
from packages.application import current_fbo_supply_close as owner
from packages.application import current_fbo_closure_evidence as evidence
from packages.application import current_fbo_supply_close_apply as admission
from packages.application import fbs_scoped_lifecycle_production as shared
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
from packages.application.fbs_accounting_runtime import writer_lock

CONTRACT = evidence.CONTRACT


def snapshot_material(conn, closure_operation_id, supplies):
    """Logical artifact hash, not a hash of a multi-gigabyte SQLite container."""
    receipt = conn.execute(f'SELECT * FROM {owner.RECEIPTS} WHERE operation_id=?', (closure_operation_id,)).fetchone()
    owner.require(receipt is not None, 'closure_snapshot_receipt_missing')
    return dict(receipt=dict(receipt), supplies=[dict(evidence.source_row(conn, s)) for s in sorted(supplies)])


def export_snapshot(source_db, closure_operation_id, supplies, destination, provenance):
    """Export unchanged selected rows from a trusted SQLite snapshot, read-only.

    The private bounded artifact can be transferred instead of the original
    database. Its logical digest is the same as direct SQLite import. Review
    the returned digest and provenance before authorizing the server request.
    """
    with closing(shared.readonly(source_db)) as conn:
        conn.execute('BEGIN')
        material = snapshot_material(conn, closure_operation_id, supplies)
        plan = json.loads(material['receipt']['result_json'])['plan']
        owner.verify(conn, plan, check_publication=False)
        for supply, row in zip(sorted(supplies), material['supplies']):
            effect = next(e for e in plan['effects'] if e['supply_id'] == supply)
            evidence.payload(plan, effect, owner._wb_supply_record_from_row(row), provenance)
    artifact = owner.canonical(dict(contract=CONTRACT, material=material, provenance=provenance)).encode()
    owner.require(len(artifact) <= 16*1024**2, 'closure_evidence_artifact_budget')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(artifact)
    return dict(path=str(Path(destination).resolve()), source_digest=owner.digest(material), provenance=provenance)


def read_snapshot(artifact, closure, supplies, attestation):
    if artifact.suffix.lower() == '.json':
        owner.require(artifact.stat().st_size <= 16*1024**2, 'closure_evidence_artifact_budget')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                owner.require(key not in result, 'closure_evidence_duplicate_json_key')
                result[key] = value
            return result
        value = json.loads(artifact.read_text(), object_pairs_hook=unique)
        owner.require(value.get('contract') == CONTRACT and value.get('provenance') == attestation,
            'closure_evidence_artifact_provenance_conflict')
        material = value['material']
        owner.require(material['receipt']['operation_id'] == closure
            and sorted(r['supply_id'] for r in material['supplies']) == sorted(supplies), 'closure_evidence_artifact_scope_conflict')
        return material
    with closing(shared.readonly(artifact)) as old:
        old.execute('BEGIN')
        material = snapshot_material(old, closure, supplies)
        owner.verify(old, json.loads(material['receipt']['result_json'])['plan'], check_publication=False)
        return material


class CurrentFboClosureEvidenceAdapter:
    def context(self, request, operation_id):
        root, db = shared.target(request)
        shared._paths(root, operation_id)
        owner.require(all(isinstance(request.get(k), str) and 0 < len(request[k]) <= maximum
            for k, maximum in (('actor',160), ('approval_reference',1000))), 'closure_evidence_approval_actor_required')
        owner.require(operation_id != request.get('closure_operation_id'), 'closure_evidence_separate_import_operation_required')
        owner.require(len(owner.canonical(request).encode()) <= 100_000, 'closure_evidence_request_budget')
        return root, db

    def candidate(self, conn, request):
        closure = request.get('closure_operation_id')
        supplies = request.get('supply_ids')
        owner.require(isinstance(supplies, list) and 1 <= len(supplies) <= 3
            and all(isinstance(s, str) and s for s in supplies) and len(set(supplies)) == len(supplies), 'closure_evidence_supply_scope')
        saved = conn.execute(f'SELECT * FROM {owner.RECEIPTS} WHERE operation_id=?', (closure,)).fetchone()
        owner.require(saved is not None, 'closure_evidence_original_receipt_missing')
        plan = json.loads(saved['result_json'])['plan']
        owner.require(plan['plan_digest'] == owner.digest({k:v for k,v in plan.items() if k != 'plan_digest'}), 'closure_evidence_original_plan_changed')
        owner.verify(conn, plan, check_publication=False)
        snapshot = request.get('trusted_snapshot') or {}
        attestation = snapshot.get('provenance') or {}
        owner.require(attestation.get('attests_original_physical_fields') is True
            and all(isinstance(attestation.get(k), str) and 0 < len(attestation[k]) <= 1000
                for k in ('origin', 'captured_at', 'approved_by', 'approval_reference')),
            'closure_evidence_trusted_snapshot_attestation_required')
        artifact = Path(snapshot.get('path', '')).resolve()
        owner.require(artifact.is_file(), 'closure_evidence_snapshot_missing')
        material = read_snapshot(artifact, closure, supplies, attestation)
        source_digest = owner.digest(material)
        owner.require(source_digest == snapshot.get('source_digest'), 'closure_evidence_snapshot_digest_changed')
        owner.require(material['receipt'] == dict(saved), 'closure_evidence_snapshot_receipt_conflict')
        values, prestate = [], []
        for supply in sorted(supplies):
            selected = [e for e in plan['effects'] if e['supply_id'] == supply]
            owner.require(len(selected) == 1, 'closure_evidence_original_supply_missing')
            effect = selected[0]
            row = next(r for r in material['supplies'] if r['supply_id'] == supply)
            record = owner._wb_supply_record_from_row(row)
            value = evidence.payload(plan, effect, record, dict(kind='approved_legacy_snapshot',
                source_path=str(artifact), source_digest=source_digest, source_row_digest=owner.digest(row),
                receipt_digest=owner.digest(material['receipt']), attestation=attestation))
            current = dict(evidence.source_row(conn, supply))
            evidence.verify_closure(conn, record=owner._wb_supply_record_from_row(current), plan=plan, effect=effect, baseline=value)
            previous = evidence.lookup(conn, closure, supply)
            owner.require(previous is None or previous == value, 'closure_evidence_conflict')
            values.append(value)
            prestate.append(dict(current=current, previous=previous))
        candidate = owner.digest(dict(contract=CONTRACT, actor=request['actor'], approval_reference=request['approval_reference'], values=values))
        state = owner.digest(dict(receipt=dict(saved), sources=prestate, code=admission.authority()))
        return values, state, candidate

    def preview(self, request, operation_id):
        _, db = self.context(request, operation_id)
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            values, state, candidate = self.candidate(conn, request)
        return dict(operation_id=operation_id, target=str(db),
            scope=dict(action=CONTRACT, closure_operation_id=request['closure_operation_id'], supply_ids=sorted(request['supply_ids'])),
            prestate_sha256=state, candidate_sha256=candidate,
            recovery=dict(method='readback_same_operation', append_only_evidence=True, monetary_writes=0, wb_calls=0),
            summary=dict(evidence=len(values), old_full_revisions={v['supply_id']:v['old_full_revision'] for v in values}))

    def apply(self, request, operation_id, preview):
        root, db = self.context(request, operation_id)
        prior = self.readback(request, operation_id)
        if prior['state'] == 'applied':
            return dict(operation_id=operation_id, disposition='already_applied')
        with admission.held_admitted(root, request), warehouse_functional_job_lock(root), warehouse_functional_write_lock(root), writer_lock(root):
            with closing(sqlite3.connect(db, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN IMMEDIATE')
                try:
                    owner.require(shared.target(request) == (root, db), 'closure_evidence_target_changed')
                    values, state, candidate = self.candidate(conn, request)
                    owner.require(state == preview['prestate_sha256'] and candidate == preview['candidate_sha256'], 'closure_evidence_preview_compare_and_swap')
                    evidence.migrate(conn)
                    # Another process may have finished this operation while we
                    # waited for the existing domain locks. No blind resubmit.
                    prior = conn.execute(f'SELECT * FROM {evidence.IMPORTS} WHERE operation_id=?', (operation_id,)).fetchone()
                    owner.require(prior is None, 'closure_evidence_concurrent_operation_readback_only')
                    created = sum(evidence.append(conn, value, operation_id) for value in values)
                    receipt = dict(operation_id=operation_id, state='applied', candidate_sha256=candidate,
                        closure_operation_id=request['closure_operation_id'], supply_ids=sorted(request['supply_ids']),
                        evidence_digests=[owner.digest(v) for v in values], created=created,
                        registered_at=datetime.now(timezone.utc).isoformat(), monetary_writes=0, wb_calls=0)
                    conn.execute(f'INSERT INTO {evidence.IMPORTS} VALUES(?,?,?,?)',
                        (operation_id, owner.digest(request), candidate, owner.canonical(receipt)))
                    # Receipt verification is native and deliberately precedes
                    # commit; additive evidence must leave native money intact.
                    plan = json.loads(conn.execute(f'SELECT result_json FROM {owner.RECEIPTS} WHERE operation_id=?',
                        (request['closure_operation_id'],)).fetchone()[0])['plan']
                    owner.verify(conn, plan, check_publication=False)
                    try:
                        conn.commit()
                    except BaseException as exc:
                        raise AmbiguousSubmit('closure_evidence_commit_readback_only') from exc
                except BaseException:
                    conn.rollback()
                    raise
        return dict(operation_id=operation_id, disposition='submitted')

    def readback(self, request, operation_id):
        _, db = self.context(request, operation_id)
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            row = conn.execute(f'SELECT * FROM {evidence.IMPORTS} WHERE operation_id=?', (operation_id,)).fetchone() if evidence.exists(conn, evidence.IMPORTS) else None
            if row is None:
                return dict(operation_id=operation_id, state='not_submitted')
            owner.require(row['request_digest'] == owner.digest(request), 'closure_evidence_existing_request_conflict')
            receipt = json.loads(row['receipt_json'])
            owner.require(receipt['candidate_sha256'] == row['candidate_digest'], 'closure_evidence_import_receipt_conflict')
            actual = [evidence.lookup(conn, request['closure_operation_id'], s) for s in sorted(request['supply_ids'])]
            owner.require(all(actual) and [owner.digest(v) for v in actual] == receipt['evidence_digests'], 'closure_evidence_import_incomplete')
            plan = json.loads(conn.execute(f'SELECT result_json FROM {owner.RECEIPTS} WHERE operation_id=?',
                (request['closure_operation_id'],)).fetchone()[0])['plan']
            owner.verify(conn, plan, check_publication=False)
        return receipt
