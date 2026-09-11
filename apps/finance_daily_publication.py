"""One-submit publication of one verified daily Finance report; no source fetch."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3

from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly, private_json
from packages.application.finance_daily_publication import assemble, project, roster
from packages.application.web_vitrina_management_history import digest
from packages.application.ready_publication import ExpectedReady, replace_ready, capture_authority
from packages.application.sheet_vitrina_v1_live_plan import TEMPORAL_ROLE_ACCEPTED_CLOSED
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.business_time import current_business_date_iso

CONTROL_FILES = ('.business-data-write-barrier.json', '.auto-updates-policy.json',
                 '.business-data-maintenance.json', '.warehouse-functional-maintenance.json')


class FinanceDailyPublicationAdapter:
    def __init__(self, now_factory=None):
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def target(self, request, *, read_only=False):
        runtime, db = WebVitrinaManagementHistoryAdapter().target(request)
        if socket.gethostname() != request['hostname']:
            raise ValueError('finance-target-host-mismatch')
        for name in (() if read_only else CONTROL_FILES):
            value = json.loads((runtime / name).read_text())
            if digest(value) != request['control_digests'][name]:
                raise ValueError('finance-control-drift:' + name)
            if name != '.auto-updates-policy.json' and value.get('active') is True:
                raise ValueError('finance-control-held:' + name)
        return runtime, db

    def build(self, request, operation_id, conn):
        runtime, db = self.target(request)
        path = Path(request['source_path']).resolve()
        if not path.is_relative_to(runtime / 'private-evidence') or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('finance-private-source-path-invalid')
        source = json.loads(path.read_text())
        if digest(source) != request['source_sha256']:
            raise ValueError('finance-source-artifact-drift')
        day = request['date']
        now = self.now_factory()
        prepared = datetime.fromisoformat(request['prepared_at'].replace('Z', '+00:00'))
        if (source['date'] != day or prepared.tzinfo is None or prepared > now
                or day >= current_business_date_iso(now)):
            raise ValueError('finance-exact-closed-date-required')
        registry = dict(conn.execute('SELECT * FROM registry_upload_current_state WHERE slot=1').fetchone())
        if registry['bundle_version'] != request['bundle_version']:
            raise ValueError('finance-active-bundle-drift')
        record = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                              (request['bundle_version'], day)).fetchone()
        if record is None or record['snapshot_id'] != request['snapshot_id']:
            raise ValueError('finance-dated-ready-identity-drift')
        before = dict(record); plan = json.loads(before['plan_json'])
        nm_ids = roster(plan)
        if nm_ids != request['roster_nm_ids']:
            raise ValueError('finance-dated-roster-drift')
        result = assemble(source, nm_ids)
        observed = datetime.fromisoformat(result.diagnostics['source_observed_at'].replace('Z', '+00:00'))
        if observed > prepared:
            raise ValueError('finance-source-observed-after-preparation')
        slots = [dict(r) for r in conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key='fin_report_daily' AND snapshot_date=? ORDER BY snapshot_role", (day,))]
        closures = [dict(r) for r in conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key='fin_report_daily' AND target_date=? ORDER BY slot_kind", (day,))]
        if any(r['snapshot_role'] == TEMPORAL_ROLE_ACCEPTED_CLOSED for r in slots):
            raise ValueError('finance-existing-accepted-closed-preserved')
        projection = project(plan, result, operation_id)
        payload = json.dumps(projection['normalized_payload'], ensure_ascii=False, separators=(',', ':'))
        old = next((r for r in closures if r['slot_kind'] == 'yesterday_closed'), {})
        closure = {'source_key': 'fin_report_daily', 'target_date': day, 'slot_kind': 'yesterday_closed',
            'state': 'success', 'attempt_count': max(1, int(old.get('attempt_count') or 0)), 'next_retry_at': None,
            'last_reason': 'finance_daily_report_publication:' + operation_id, 'last_attempt_at': request['prepared_at'],
            'last_success_at': request['prepared_at'], 'accepted_at': request['prepared_at']}
        ready_after = {**before, 'plan_json': json.dumps(projection['plan'], ensure_ascii=False, separators=(',', ':')),
                       'refreshed_at': request['prepared_at']}
        prestate = {'ready': before, 'slots': slots, 'closures': closures, 'registry': registry,
                    'authority': capture_authority(runtime, db_path=db)}
        return {'operation_id': operation_id, 'date': day, 'prestate_sha256': digest(prestate),
            'source_sha256': request['source_sha256'], 'before_image': prestate,
            'slot_after': {'source_key': 'fin_report_daily', 'snapshot_date': day,
                'snapshot_role': TEMPORAL_ROLE_ACCEPTED_CLOSED, 'captured_at': request['prepared_at'], 'payload_json': payload},
            'closure_after': closure, 'ready_after': ready_after, 'ready_after_json': ready_after['plan_json'],
            'changes': projection['changes'], 'target_keys': projection['target_keys'],
            'non_target_digest': projection['non_target_digest'], 'roster_nm_ids': nm_ids}

    def preview(self, request, operation_id):
        runtime, db = self.target(request)
        backup = runtime / 'evidence' / (operation_id + '.finance-before.json')
        if backup.exists():
            retained = json.loads(backup.read_text())
            if retained['request_sha256'] != digest(request):
                raise ValueError('finance-operation-request-drift')
            candidate = retained['candidate']
        else:
            with readonly(db) as conn:
                conn.execute('BEGIN')
                candidate = self.build(request, operation_id, conn)
        return {'operation_id': operation_id, 'target': str(db),
            'scope': {'date': candidate['date'], 'source': 'fin_report_daily', 'source_slots': 1, 'closure_rows': 1,
                      'ready_as_of_date': candidate['date'], 'target_cells': len(candidate['target_keys']),
                      'changed_cells': sum(c['before'] != c['after'] for c in candidate['changes'])},
            'prestate_sha256': candidate['prestate_sha256'], 'candidate_sha256': digest(candidate),
            'recovery': {'kind': 'exact-before-images-and-atomic-rollback', 'path': str(backup)}, 'candidate': candidate}

    def apply(self, request, operation_id, preview):
        runtime, db = self.target(request)
        with warehouse_functional_job_lock(runtime, blocking=False), warehouse_sync_lock(runtime, blocking=False):
            with closing(sqlite3.connect(db, timeout=10)) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN IMMEDIATE')
                candidate = self.build(request, operation_id, conn)
                if candidate['prestate_sha256'] != preview['prestate_sha256'] or digest(candidate) != preview['candidate_sha256']:
                    raise ValueError('finance-candidate-cas-drift')
                backup = runtime / 'evidence' / (operation_id + '.finance-before.json')
                backup.parent.mkdir(exist_ok=True)
                if backup.exists():
                    raise ValueError('finance-operation-attempted-use-readback')
                image = {'operation_id': operation_id, 'target': str(db), 'request_sha256': digest(request), 'candidate': candidate}
                private_json(backup, image)
                if digest(json.loads(backup.read_text())) != digest(image):
                    raise ValueError('finance-backup-verification-failed')
                for table, row in (('temporal_source_slot_snapshots', candidate['slot_after']),
                                   ('temporal_source_closure_state', candidate['closure_after'])):
                    conn.execute('INSERT OR REPLACE INTO ' + table + '(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))
                before = candidate['before_image']['ready']
                replace_ready(conn, expected=ExpectedReady(before['bundle_version'], before['as_of_date'], before['plan_json'], candidate['before_image']['authority']),
                              plan_json=candidate['ready_after_json'], refreshed_at=request['prepared_at'])
                conn.commit()
        return {'operation_id': operation_id, 'disposition': 'submitted'}

    def _retained(self, request, operation_id):
        runtime, db = self.target(request, read_only=True)
        path = runtime / 'evidence' / (operation_id + '.finance-before.json')
        if not path.exists():
            return runtime, db, path, None
        image = json.loads(path.read_text())
        if image['request_sha256'] != digest(request):
            raise ValueError('finance-operation-request-drift')
        return runtime, db, path, image['candidate']

    @staticmethod
    def _current(conn, candidate):
        before = candidate['before_image']['ready']; day = candidate['date']
        ready = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                             (before['bundle_version'], before['as_of_date'])).fetchone()
        slot = conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key='fin_report_daily' AND snapshot_date=? AND snapshot_role=?", (day, TEMPORAL_ROLE_ACCEPTED_CLOSED)).fetchone()
        closure = conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key='fin_report_daily' AND target_date=? AND slot_kind='yesterday_closed'", (day,)).fetchone()
        return (dict(r) if r is not None else None for r in (ready, slot, closure))

    def readback(self, request, operation_id):
        _, db, path, candidate = self._retained(request, operation_id)
        if candidate is None:
            return {'operation_id': operation_id, 'state': 'not_submitted'}
        with readonly(db) as conn:
            conn.execute('BEGIN')
            ready, slot, closure = self._current(conn, candidate)
        before = candidate['before_image']; bad = []
        if ready != candidate['ready_after']: bad.append('ready')
        if slot != candidate['slot_after']: bad.append('slot')
        if closure != candidate['closure_after']: bad.append('closure')
        original_slot = next((r for r in before['slots'] if r['snapshot_role'] == TEMPORAL_ROLE_ACCEPTED_CLOSED), None)
        original_closure = next((r for r in before['closures'] if r['slot_kind'] == 'yesterday_closed'), None)
        unsubmitted = ready == before['ready'] and slot == original_slot and closure == original_closure
        return {'operation_id': operation_id, 'state': 'applied' if not bad else 'not_submitted' if unsubmitted else 'ambiguous',
            'mismatches': bad, 'verified_cells': len(candidate['target_keys']) if not bad else 0,
            'date': candidate['date'], 'source_sha256': candidate['source_sha256'], 'recovery_path': str(path)}

    def rollback(self, request, operation_id):
        runtime, db, _, candidate = self._retained(request, operation_id)
        if candidate is None: raise ValueError('finance-recovery-image-missing')
        before = candidate['before_image']; receipt = runtime / 'evidence' / (operation_id + '.finance-recovery.json')
        with warehouse_functional_job_lock(runtime, blocking=False), warehouse_sync_lock(runtime, blocking=False):
            with closing(sqlite3.connect(db, timeout=10)) as conn, conn:
                conn.row_factory = sqlite3.Row; conn.execute('BEGIN IMMEDIATE'); self.target(request)
                current, slot, closure = self._current(conn, candidate)
                if (current != candidate['ready_after']
                        or slot != candidate['slot_after'] or closure != candidate['closure_after']):
                    raise ValueError('finance-recovery-after-image-drift')
                if receipt.exists(): raise ValueError('finance-recovery-already-attempted')
                private_json(receipt, {'operation_id': operation_id, 'candidate_sha256': digest(candidate)})
                replace_ready(conn, expected=ExpectedReady(current['bundle_version'], current['as_of_date'], current['plan_json'], before['authority']),
                              plan_json=before['ready']['plan_json'], refreshed_at=before['ready']['refreshed_at'])
                for table, originals, after, keys in (
                    ('temporal_source_slot_snapshots', before['slots'], candidate['slot_after'], ('source_key', 'snapshot_date', 'snapshot_role')),
                    ('temporal_source_closure_state', before['closures'], candidate['closure_after'], ('source_key', 'target_date', 'slot_kind'))):
                    original = next((r for r in originals if all(r[k] == after[k] for k in keys)), None)
                    conn.execute('DELETE FROM ' + table + ' WHERE ' + ' AND '.join(k + '=?' for k in keys), tuple(after[k] for k in keys))
                    if original:
                        conn.execute('INSERT INTO ' + table + '(' + ','.join(original) + ') VALUES(' + ','.join('?' for _ in original) + ')', tuple(original.values()))
                conn.commit()
        return {'operation_id': operation_id, 'state': 'restored', 'recovery_receipt': str(receipt)}
