"""Publish reviewed official fullstats days through the one-submit launcher.

Only absent accepted Ads slots and their closure rows can change. Recovery
keeps exact target before-images instead of copying the operational database.
No source fetch, ready-plan rebuild or Finance/COGS write happens on submit.
"""
from datetime import date
import json
import os
from pathlib import Path
import socket

from apps.web_vitrina_management_history import (
    WebVitrinaManagementHistoryAdapter, private_json,
)
from apps.production_apply_contract import AmbiguousSubmit
from packages.application.ads_historical_recovery import (
    AdsHistoricalRecovery, AdsHistoricalRecoveryScope, canonical_digest,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_job_lock, warehouse_functional_write_lock,
)


class AdsClosedDayRecoveryAdapter:
    def context(self, request):
        runtime, db = WebVitrinaManagementHistoryAdapter().target(request)
        if socket.gethostname() != request['hostname']:
            raise ValueError('ads-recovery-host-drift')
        path = Path(request['reviewed_plan_path']).resolve()
        if not path.is_relative_to(runtime / 'evidence'):
            raise ValueError('ads-recovery-plan-outside-evidence')
        plan = json.loads(path.read_text())
        scope = AdsHistoricalRecoveryScope.build(
            nm_ids=request['nm_ids'],
            target_dates=[date.fromisoformat(d) for d in request['dates']],
        )
        if len(scope.target_dates) > 7:
            raise ValueError('ads-recovery-date-scope-too-wide')
        recovery = AdsHistoricalRecovery(db_path=db, source=None)
        plan = recovery._validate_reviewed_plan(
            scope, reviewed_plan=plan, expected_fingerprint=request['plan_fingerprint'],
        )
        if any(row['action'] != 'insert' for row in plan['target_manifest']):
            raise ValueError('ads-recovery-only-absent-days-supported')
        return runtime, db, recovery, scope, plan

    @staticmethod
    def backup_path(runtime, operation_id):
        return runtime / 'evidence' / (operation_id + '.ads-before.json')

    def preview(self, request, operation_id):
        runtime, db, recovery, scope, plan = self.context(request)
        backup = self.backup_path(runtime, operation_id)
        if backup.exists():
            retained = json.loads(backup.read_text())
            if retained['request_sha256'] != canonical_digest(request):
                raise ValueError('ads-recovery-operation-drift')
        else:
            with recovery._connect(read_only=True) as conn:
                conn.execute('BEGIN')
                self.check_prestate(recovery, conn, scope, plan)
        return {
            'operation_id': operation_id, 'target': str(db),
            'scope': {**scope.as_dict(), 'source_key': 'ads_compact',
                      'insert_snapshot_count': plan['write_set']['insert_snapshot_count']},
            'prestate_sha256': canonical_digest({
                'target': plan['target_before_digest'], 'non_target': plan['non_target_digest'],
            }),
            'candidate_sha256': plan['fingerprint'],
            'recovery': {'kind': 'exact-target-before-images-and-atomic-rollback', 'path': str(backup)},
        }

    @staticmethod
    def check_prestate(recovery, conn, scope, plan):
        before = recovery._target_state_manifest(conn, scope)
        if canonical_digest(before) != plan['target_before_digest']:
            raise ValueError('ads-recovery-target-drift')
        if canonical_digest(recovery._non_target_manifest(conn, scope)) != plan['non_target_digest']:
            raise ValueError('ads-recovery-nontarget-drift')
        return before

    def apply(self, request, operation_id, preview):
        runtime, db, recovery, scope, plan = self.context(request)
        backup = self.backup_path(runtime, operation_id)
        if backup.exists():
            raise ValueError('ads-recovery-already-attempted-use-readback')

        def save_before_images(db_path, backup_dir, *, fingerprint):
            if db_path != db or fingerprint != plan['fingerprint']:
                raise ValueError('ads-recovery-backup-scope-drift')
            with recovery._connect(read_only=True) as conn:
                conn.execute('BEGIN')
                before = self.check_prestate(recovery, conn, scope, plan)
            image = {'operation_id': operation_id, 'request_sha256': canonical_digest(request),
                     'target': str(db), 'target_before': before, 'reviewed_plan': plan}
            private_json(backup, image)
            descriptor = os.open(backup.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if json.loads(backup.read_text()) != image:
                raise ValueError('ads-recovery-backup-verification-failed')
            return {'created': True, 'path': str(backup), 'size_bytes': backup.stat().st_size,
                    'sha256': canonical_digest(image), 'kind': 'exact_target_before_images'}

        with (warehouse_functional_job_lock(runtime, blocking=False),
              warehouse_functional_write_lock(runtime, blocking=False)):
            # Pin target and prestate again under the common writer locks.
            current = self.preview(request, operation_id)
            if current != preview:
                raise ValueError('ads-recovery-preview-drift')
            recovery.backup_creator = save_before_images
            try:
                recovery.apply(scope, reviewed_plan=plan, expected_fingerprint=plan['fingerprint'],
                               approval_reference=operation_id, backup_dir=backup.parent)
            except Exception:
                if backup.exists():
                    raise AmbiguousSubmit('ads-recovery-readback-required') from None
                raise
        return {'operation_id': operation_id, 'disposition': 'submitted'}

    def readback(self, request, operation_id):
        runtime, db, recovery, scope, plan = self.context(request)
        backup = self.backup_path(runtime, operation_id)
        if not backup.exists():
            return {'operation_id': operation_id, 'state': 'not_submitted'}
        retained = json.loads(backup.read_text())
        if retained['request_sha256'] != canonical_digest(request) or retained['target'] != str(db):
            raise ValueError('ads-recovery-operation-drift')
        audit = recovery._read_prior_audit(plan['fingerprint'])
        actual = recovery.readback(scope)
        applied = bool(audit and audit['approval_reference'] == operation_id
                       and actual['status'] == 'ready'
                       and actual['snapshot_digest'] == audit['snapshot_digest'])
        return {'operation_id': operation_id, 'state': 'applied' if applied else 'ambiguous',
                'snapshot_digest': actual['snapshot_digest'], 'blockers': actual['blockers']}
