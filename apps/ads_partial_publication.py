"""Reusable one-submit publication of retained partial Ads and dependent ready cells."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3

from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly, private_json
from packages.application.web_vitrina_management_history import digest, dated_parameters
from packages.application.ads_partial_publication import assemble, project
from packages.application.registry_upload_db_backed_runtime import _load_metric_items, _load_formula_items, _load_config_items
from packages.application.ready_publication import ExpectedReady, replace_ready
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item

CONTROL_FILES = ('.business-data-write-barrier.json', '.auto-updates-policy.json',
                 '.business-data-maintenance.json', '.warehouse-functional-maintenance.json')


class AdsPartialPublicationAdapter:
    def __init__(self, now_factory=None):
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def target(self, request, *, read_only=False):
        runtime, db = WebVitrinaManagementHistoryAdapter().target(request)
        if socket.gethostname() != request['hostname']:
            raise ValueError('ads-target-host-mismatch')
        for name in (() if read_only else CONTROL_FILES):
            value = json.loads((runtime / name).read_text())
            if digest(value) != request['control_digests'][name]:
                raise ValueError('ads-control-drift:' + name)
            if name != '.auto-updates-policy.json' and value.get('active') is True:
                raise ValueError('ads-control-held:' + name)
        return runtime, db

    def build(self, request, operation_id, conn):
        source = request['source']; day = source['date']
        if digest(source) != request['source_sha256']:
            raise ValueError('ads-source-drift')
        prepared = datetime.fromisoformat(request['prepared_at'].replace('Z', '+00:00'))
        if prepared.tzinfo is None:
            raise ValueError('ads-prepared-clock-invalid')
        actual_now = self.now_factory()
        from packages.business_time import current_business_date_iso
        if prepared > actual_now or day >= current_business_date_iso(actual_now):
            raise ValueError('ads-retained-publication-requires-closed-date')
        registry = dict(conn.execute('SELECT * FROM registry_upload_current_state WHERE slot=1').fetchone())
        if registry['bundle_version'] != request['bundle_version']:
            raise ValueError('ads-registry-drift')
        bundle = registry['bundle_version']
        metrics = {m.metric_key:m for m in _load_metric_items(conn, bundle)}
        formulas = {f.formula_id:f for f in _load_formula_items(conn, bundle)}
        manual = {c.nm_id:c for c in _load_config_items(conn, bundle)}
        record = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                              (bundle, request['ready_as_of_date'])).fetchone()
        if record is None: raise ValueError('ads-ready-target-missing')
        before = dict(record); plan = json.loads(before['plan_json'])
        sheet = next(s for s in plan['sheets'] if s['sheet_name'] == 'DATA_VITRINA')
        if day not in sheet['header']: raise ValueError('ads-ready-date-missing')
        nms = sorted({int(r[1].split('|')[0][4:]) for r in sheet['rows'] if r[1].startswith('SKU:')})
        config = [manual.get(n) or ConfigV2Item(n, True, str(n), '', i) for i,n in enumerate(nms)]
        slots = [dict(r) for r in conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key='ads_compact' AND snapshot_date=? ORDER BY snapshot_role", (day,))]
        closures = [dict(r) for r in conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key='ads_compact' AND target_date=? ORDER BY slot_kind", (day,))]
        for item in slots:
            from packages.application.ads_snapshot_payload import resolve_ads_snapshot_payload
            existing, _ = resolve_ads_snapshot_payload(json.loads(item['payload_json']));kind = (existing or {}).get('kind')
            if kind in ('success', 'empty'):
                raise ValueError('ads-complete-snapshot-preserved')
        result = assemble(source, nms)
        observed = datetime.fromisoformat(result.diagnostics['source_observed_at'].replace('Z','+00:00'))
        if observed > prepared: raise ValueError('ads-source-clock-after-preparation')
        params = dated_parameters(conn, day)
        if params is None: raise ValueError('ads-dated-parameters-missing')
        revised = project(plan, result=result, config=config, metrics=metrics, formulas=formulas,
                          parameters=params, operation_id=operation_id)
        index = sheet['header'].index(day)
        after_sheet = next(s for s in revised['sheets'] if s['sheet_name']=='DATA_VITRINA')
        after_rows = {r[1]:r for r in after_sheet['rows']}
        from packages.application.metric_completeness import ads_dependencies
        affected = ads_dependencies(metrics, formulas)
        changes = []
        for row in sheet['rows']:
            after = after_rows[row[1]]
            for col, value in enumerate(row):
                if value == after[col]: continue
                if col != index or row[1].split('|')[1] not in affected:
                    raise ValueError('ads-nontarget-cell-changed')
                changes.append({'row_id':row[1], 'date':day, 'before':value, 'after':after[col]})
        payload = json.dumps(asdict(result),ensure_ascii=False,separators=(',',':'))
        from packages.application.sheet_vitrina_v1_live_plan import _next_closure_retry
        retry_at, retry_state = _next_closure_retry(prepared, 0, 'retained_partial_observation_not_complete')
        closure = {'source_key':'ads_compact','target_date':day,'slot_kind':'yesterday_closed',
            'state':retry_state,'attempt_count':0,'next_retry_at':retry_at,
            'last_reason':'retained_partial_observation_not_complete', 'last_attempt_at':None,
            'last_success_at':None, 'accepted_at':request['prepared_at']}
        prestate = {'ready':before,'slots':slots,'closures':closures,'registry':registry,
                    'metrics':[asdict(x) for x in metrics.values()], 'formulas':[asdict(x) for x in formulas.values()],
                    'config':[asdict(x) for x in manual.values()], 'parameters':[asdict(x) for x in params]}
        # Parameter Decimals are canonicalized explicitly, not silently discarded.
        prestate['parameters'] = json.loads(json.dumps(prestate['parameters'],default=str,sort_keys=True))
        return {'operation_id':operation_id,'date':day,'prestate_sha256':digest(prestate),
            'source_sha256':request['source_sha256'],'before_image':prestate,
            'slot_after':{'source_key':'ads_compact','snapshot_date':day,'snapshot_role':'accepted_closed_day_snapshot',
                          'captured_at':request['prepared_at'],'payload_json':payload},
            'closure_after':closure,'ready_after_json':json.dumps(revised,ensure_ascii=False,separators=(',',':')),
            'changes':changes,'observed_campaign_count':len(result.diagnostics['observed_campaign_ids']),
            'unresolved_campaign_ids':result.diagnostics['unresolved_campaign_ids']}

    def preview(self, request, operation_id):
        runtime, db = self.target(request)
        backup = runtime / 'evidence' / (operation_id + '.ads-before.json')
        if backup.exists():
            retained = json.loads(backup.read_text())
            if retained['request_sha256'] != digest(request): raise ValueError('ads-operation-request-drift')
            candidate = retained['candidate']
        else:
            with readonly(db) as conn:
                conn.execute('BEGIN')
                candidate = self.build(request, operation_id, conn)
        return {'operation_id':operation_id,'target':str(db),
            'scope':{'date':candidate['date'],'source':'ads_compact','ready_as_of_date':request['ready_as_of_date'],
                     'changed_cells':len(candidate['changes']),'source_slots':1,'closure_rows':1},
            'prestate_sha256':candidate['prestate_sha256'],'candidate_sha256':digest(candidate),
            'recovery':{'kind':'exact-before-images-and-atomic-rollback','path':str(backup)},'candidate':candidate}

    def apply(self, request, operation_id, preview):
        runtime, db = self.target(request)
        with warehouse_functional_job_lock(runtime,blocking=False), warehouse_sync_lock(runtime,blocking=False):
            with closing(sqlite3.connect(db,timeout=10)) as conn, conn:
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');self.target(request)
                candidate=self.build(request,operation_id,conn)
                if candidate['prestate_sha256'] != preview['prestate_sha256'] or digest(candidate) != preview['candidate_sha256']:
                    raise ValueError('ads-candidate-cas-drift')
                backup=runtime/'evidence'/(operation_id+'.ads-before.json');backup.parent.mkdir(exist_ok=True)
                if backup.exists(): raise ValueError('ads-operation-attempted-use-readback')
                image={'operation_id':operation_id,'target':str(db),'request_sha256':digest(request),'candidate':candidate}
                private_json(backup,image)
                if digest(json.loads(backup.read_text())) != digest(image): raise ValueError('ads-backup-verification-failed')
                slot=candidate['slot_after'];closure=candidate['closure_after']
                conn.execute('INSERT OR REPLACE INTO temporal_source_slot_snapshots('+','.join(slot)+') VALUES('+','.join('?' for _ in slot)+')',tuple(slot.values()))
                conn.execute('INSERT OR REPLACE INTO temporal_source_closure_state('+','.join(closure)+') VALUES('+','.join('?' for _ in closure)+')',tuple(closure.values()))
                before=candidate['before_image']['ready']
                replace_ready(conn,expected=ExpectedReady(before['bundle_version'],before['as_of_date'],before['plan_json']),
                              plan_json=candidate['ready_after_json'],refreshed_at=request['prepared_at'])
                conn.commit()
        return {'operation_id':operation_id,'disposition':'submitted'}

    def rollback(self, request, operation_id):
        """Restore only this operation's exact after-images; a later cycle wins."""
        runtime, db = self.target(request)
        path = runtime/'evidence'/(operation_id+'.ads-before.json')
        image = json.loads(path.read_text())
        if image['request_sha256'] != digest(request): raise ValueError('ads-operation-request-drift')
        candidate = image['candidate']; before = candidate['before_image']; day = candidate['date']
        receipt = runtime/'evidence'/(operation_id+'.ads-recovery.json')
        with warehouse_functional_job_lock(runtime,blocking=False), warehouse_sync_lock(runtime,blocking=False):
            with closing(sqlite3.connect(db,timeout=10)) as conn, conn:
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');self.target(request)
                current = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                    (before['ready']['bundle_version'],before['ready']['as_of_date'])).fetchone()
                slot = conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key='ads_compact' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'",(day,)).fetchone()
                closure = conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key='ads_compact' AND target_date=? AND slot_kind='yesterday_closed'",(day,)).fetchone()
                if (not current or current['plan_json'] != candidate['ready_after_json']
                        or not slot or dict(slot) != candidate['slot_after']
                        or not closure or dict(closure) != candidate['closure_after']):
                    raise ValueError('ads-recovery-after-image-drift')
                if receipt.exists(): raise ValueError('ads-recovery-already-attempted')
                private_json(receipt,{'operation_id':operation_id,'candidate_sha256':digest(candidate),'kind':'exact-after-image-cas-restore'})
                replace_ready(conn,expected=ExpectedReady(current['bundle_version'],current['as_of_date'],current['plan_json']),
                    plan_json=before['ready']['plan_json'],refreshed_at=before['ready']['refreshed_at'])
                for table, originals, after in (('temporal_source_slot_snapshots',before['slots'],candidate['slot_after']),
                                                ('temporal_source_closure_state',before['closures'],candidate['closure_after'])):
                    keys = (['source_key','snapshot_date','snapshot_role'] if table == 'temporal_source_slot_snapshots'
                            else ['source_key','target_date','slot_kind'])
                    original = next((r for r in originals if all(r[k]==after[k] for k in keys)),None)
                    conn.execute('DELETE FROM '+table+' WHERE '+' AND '.join(k+'=?' for k in keys),tuple(after[k] for k in keys))
                    if original:
                        conn.execute('INSERT INTO '+table+'('+','.join(original)+') VALUES('+','.join('?' for _ in original)+')',tuple(original.values()))
                conn.commit()
        return {'operation_id':operation_id,'state':'restored','recovery_receipt':str(receipt)}

    def readback(self, request, operation_id):
        runtime, db = self.target(request, read_only=True);path=runtime/'evidence'/(operation_id+'.ads-before.json')
        if not path.exists(): return {'operation_id':operation_id,'state':'not_submitted'}
        image=json.loads(path.read_text())
        if image['request_sha256'] != digest(request): raise ValueError('ads-operation-request-drift')
        candidate=image['candidate'];before=candidate['before_image'];bad=[]
        with readonly(db) as conn:
            row=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                             (before['ready']['bundle_version'],before['ready']['as_of_date'])).fetchone()
            slot=conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key='ads_compact' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'",(candidate['date'],)).fetchone()
            closure=conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key='ads_compact' AND target_date=? AND slot_kind='yesterday_closed'",(candidate['date'],)).fetchone()
            if not row or row[0] != candidate['ready_after_json']:bad.append('ready')
            if not slot or dict(slot) != candidate['slot_after']:bad.append('slot')
            if not closure or dict(closure) != candidate['closure_after']:bad.append('closure')
            old_slot = next((r for r in before['slots'] if r['snapshot_role']=='accepted_closed_day_snapshot'),None)
            old_closure = next((r for r in before['closures'] if r['slot_kind']=='yesterday_closed'),None)
            rolled_back = (row is not None and row[0] == before['ready']['plan_json']
                and (dict(slot) if slot else None) == old_slot and (dict(closure) if closure else None) == old_closure)
        return {'operation_id':operation_id,'state':'applied' if not bad else 'not_submitted' if rolled_back else 'ambiguous','mismatches':bad,
                'source_sha256':candidate['source_sha256'],'date':candidate['date'],'recovery_path':str(path)}
