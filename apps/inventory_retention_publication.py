"""Certify preserved inventory after a completed scoped ready publication.

This reusable domain adapter adds one compact acceptance journal record. It
never changes a ready plan, inventory component, book, finalization or source.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter
from packages.application.ready_publication import (
    readonly, digest, canonical, ExpectedReady, record_intent, complete_publication,
)
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.inventory_quantity import CONTRACT, resolve_plan_quantities
from packages.application.management_inventory_history import verify_capture_content
from packages.application.sheet_vitrina_v1_inventory_history import CAPTURES_TABLE, preview_inventory_history_capture
from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
from packages.application.warehouse_sync_lock import warehouse_sync_lock


class InventoryRetentionPublicationAdapter(WebVitrinaManagementHistoryAdapter):
    def candidate(self, request, operation_id, conn):
        runtime, db = self.target(request)
        evidence_path = Path(request['transition_evidence'])
        if evidence_path.resolve().parent != runtime / 'evidence':
            raise ValueError('inventory-transition-evidence-path-invalid')
        raw = evidence_path.read_bytes()
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != request['transition_evidence_sha256']:
            raise ValueError('inventory-transition-evidence-drift')
        recovery = json.loads(raw)
        transition = recovery['candidate']
        if (fingerprint(transition) != request['transition_candidate_sha256']
                or transition['operation_id'] != request['transition_operation_id']
                or transition['phase'] != 'publication'):
            raise ValueError('inventory-transition-candidate-invalid')
        target = request['ready_target']
        key = (target['bundle_version'], target['as_of_date'])
        def select(records):
            matches = [r for r in records if (r['bundle_version'], r['as_of_date']) == key]
            if len(matches) != 1:
                raise ValueError('inventory-transition-target-invalid')
            return matches[0]
        before = select(transition['before_images']['ready'])
        after = select(transition['after_images']['ready'])
        actual = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?', key).fetchone()
        if actual is None or dict(actual) != after:
            raise ValueError('inventory-transition-ready-drift')
        old, new = [_deserialize_sheet_vitrina_plan(r['plan_json']) for r in (before, after)]
        dates = sorted(set(request['dates']))
        if not dates or len(dates) > 31:
            raise ValueError('inventory-transition-date-scope-invalid')
        pointer = {'contract': 'accepted_inventory_retention_v1', 'ready_target': target,
            'transition_operation_id': transition['operation_id'],
            'transition_candidate_sha256': request['transition_candidate_sha256'],
            'transition_evidence_sha256': request['transition_evidence_sha256'],
            'before_content_digest': digest(before['plan_json']), 'after_content_digest': digest(after['plan_json']), 'dates': {}}
        for day in dates:
            binding = old.metadata.get('fbs_accounting_bindings', {}).get(day)
            if (not binding or binding != new.metadata.get('fbs_accounting_bindings', {}).get(day)
                    or day not in old.date_columns or day not in new.date_columns
                    or old.metadata.get('ready_publication_target') != target
                    or new.metadata.get('ready_publication_target') != target):
                raise ValueError('inventory-transition-binding-changed')
            def inventory_slice(plan):
                keys = {str(row[1]) for sheet in plan.sheets if sheet.sheet_name == 'DATA_VITRINA'
                    for row in sheet.rows if str(row[1]).split('|')[-1].removeprefix('total_') == 'stock_total'
                    or str(row[1]).split('|')[-1].removeprefix('total_').startswith('inventory_')}
                values = {str(row[1]): row[2 + plan.date_columns.index(day)]
                    for sheet in plan.sheets if sheet.sheet_name == 'DATA_VITRINA' for row in sheet.rows if str(row[1]) in keys}
                cells = {key: plan.metadata.get('server_cell_presentation', {}).get(key, {}).get(day) for key in keys}
                return values, cells
            if inventory_slice(old) != inventory_slice(new):
                raise ValueError('inventory-transition-quantity-cells-changed')
            operands = resolve_plan_quantities(old, day=day, runtime_dir=runtime, ready_target=target)
            preview = preview_inventory_history_capture(business_date=day, capture_kind='accepted_refresh',
                formula_version=CONTRACT, facility_roster=operands['facility_roster'],
                source_manifest=operands['source_manifest'], components=operands['components'], captured_at=before['refreshed_at'])
            capture = conn.execute(f'SELECT * FROM {CAPTURES_TABLE} WHERE capture_id=? AND business_date=? AND source_digest=?',
                (preview['capture_id'], day, preview['source_digest'])).fetchone()
            receipt = conn.execute("""SELECT * FROM sheet_vitrina_v1_ready_publications WHERE
                bundle_version=? AND as_of_date=? AND state='complete' AND ready_required=1
                AND book_version=? AND after_digest=? ORDER BY operation_id,attempt_id LIMIT 1""",
                (*key, binding['book_version'], pointer['before_content_digest'])).fetchone()
            if (capture is None or receipt is None or capture['captured_at'] != receipt['finished_at']
                    or capture['bundle_version'] != key[0] or capture['ready_snapshot_id'] != old.snapshot_id
                    or capture['ready_plan_version'] != old.plan_version or not verify_capture_content(conn, capture)):
                raise ValueError('inventory-transition-original-acceptance-missing')
            pointer['dates'][day] = {'binding': binding, 'capture_id': capture['capture_id'],
                'source_digest': capture['source_digest'], 'publication_operation_id': receipt['operation_id'],
                'publication_content_digest': receipt['after_digest']}
        return pointer

    def preview(self, request, operation_id):
        runtime, db = self.target(request)
        with readonly(db) as conn:
            candidate = self.candidate(request, operation_id, conn)
        return {'operation_id': operation_id, 'target': str(db), 'scope': {'dates': request['dates'], 'kind': 'inventory acceptance only'},
            'prestate_sha256': fingerprint({'ready_digest': candidate['after_content_digest'], 'operation_id': operation_id}),
            'candidate_sha256': fingerprint(candidate), 'candidate': candidate,
            'recovery': {'kind': 'additive receipt; existing ready and sources unchanged', 'path': request['transition_evidence']}}

    def apply(self, request, operation_id, preview):
        runtime, db = self.target(request)
        # Resolve the large retained evidence before taking the sole writer.
        with readonly(db) as read_conn:
            candidate = self.candidate(request, operation_id, read_conn)
        if fingerprint(candidate) != preview['candidate_sha256']:
            raise ValueError('inventory-transition-cas-drift')
        with warehouse_functional_job_lock(runtime, blocking=False), warehouse_sync_lock(runtime, blocking=False), \
             warehouse_functional_write_lock(runtime, timeout_seconds=5):
            with closing(sqlite3.connect(db, timeout=5)) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN IMMEDIATE')
                self.target(request)
                target = request['ready_target']
                row = conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                    (target['bundle_version'], target['as_of_date'])).fetchone()
                if row is None or digest(row[0]) != candidate['after_content_digest']:
                    raise ValueError('inventory-transition-ready-drift')
                for day, item in candidate['dates'].items():
                    original = conn.execute("""SELECT after_digest FROM sheet_vitrina_v1_ready_publications
                        WHERE operation_id=? AND state='complete' AND bundle_version=? AND as_of_date=?
                        AND book_version=?""", (item['publication_operation_id'], target['bundle_version'],
                            target['as_of_date'], item['binding']['book_version'])).fetchone()
                    captured = conn.execute(f'SELECT source_digest FROM {CAPTURES_TABLE} WHERE capture_id=? AND business_date=?',
                        (item['capture_id'], day)).fetchone()
                    if (original is None or original[0] != item['publication_content_digest']
                            or captured is None or captured[0] != item['source_digest']):
                        raise ValueError('inventory-transition-source-drift')
                now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                version = next(iter(candidate['dates'].values()))['binding']['book_version']
                record_intent(conn, operation_id=operation_id, attempt_id='1', kind='inventory_retention',
                    expected=ExpectedReady(target['bundle_version'], target['as_of_date'], row[0]), inputs=candidate,
                    expected_book=version, book_required=False, ready_required=True, created_at=now)
                complete_publication(conn, operation_id=operation_id, attempt_id='1', book_version=version,
                    after_digest=candidate['after_content_digest'], finished_at=now)
        return {'operation_id': operation_id, 'disposition': 'submitted'}

    def readback(self, request, operation_id):
        runtime, db = self.target(request)
        with readonly(db) as conn:
            row = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?', (operation_id, '1')).fetchone()
            if row is None:
                return {'operation_id': operation_id, 'state': 'not_submitted'}
            candidate = self.candidate(request, operation_id, conn)
            okay = (row['state'] == 'complete' and row['kind'] == 'inventory_retention'
                and row['inputs_json'] == canonical(candidate) and row['after_digest'] == candidate['after_content_digest'])
        return {'operation_id': operation_id, 'state': 'applied' if okay else 'ambiguous',
            'ready_content_digest': candidate['after_content_digest'], 'dates': candidate['dates']}
