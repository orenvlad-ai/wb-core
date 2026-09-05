"""Exact historical WB capture recovery, retaining every FBS component."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Any

from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly, private_json
from packages.application.web_vitrina_management_history import digest
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.sheet_vitrina_v1_inventory_history import (
    append_inventory_history_capture, append_inventory_history_finalization,
    preview_inventory_history_capture, _stored_component,
)

CAPTURES='sheet_vitrina_v1_inventory_history_captures'
COMPONENTS='sheet_vitrina_v1_inventory_history_components'
FINALS='sheet_vitrina_v1_inventory_history_finalizations'


class WebVitrinaWbHistoryRecoveryAdapter(WebVitrinaManagementHistoryAdapter):
    def build(self, request: dict[str, Any], operation_id: str, conn: Any) -> dict[str, Any]:
        source=request['source']; day=request['target_date']
        if source.get('kind') != 'success' or source.get('snapshot_date') != day or digest(source)!=request['source_sha256']:
            raise ValueError('exact-WB-source-invalid')
        base=conn.execute('SELECT capture.* FROM '+FINALS+' finalized JOIN '+CAPTURES+' capture ON capture.capture_id=finalized.capture_id '
            'WHERE finalized.business_date=? ORDER BY finalized.finalization_sequence DESC LIMIT 1',(day,)).fetchone()
        initial_publication=base is None
        if initial_publication:
            if request.get('allow_initial_publication') is not True:
                raise ValueError('same-date-published-inventory-capture-missing')
            base=conn.execute('SELECT * FROM '+CAPTURES+' WHERE business_date=? ORDER BY capture_sequence DESC LIMIT 1',(day,)).fetchone()
            if base is None: raise ValueError('same-date-inventory-capture-missing')
        columns='scope_kind,scope_key,nm_id,component_kind,component_id,component_label,state,quantity,source_revision,source_digest,source_watermark,provenance_json'
        stored=conn.execute('SELECT '+columns+' FROM '+COMPONENTS+' WHERE capture_id=? ORDER BY scope_kind,scope_key,component_kind,component_id',(base['capture_id'],)).fetchall()
        before=[_stored_component(r) for r in stored]; after=deepcopy(before)
        if initial_publication and (not before or any(c['state']!='missing' or c['quantity'] is not None for c in before)):
            raise ValueError('initial-publication-would-expose-unpublished-components')
        values={f"SKU:{int(i['nm_id'])}":i['stock_total'] for i in source['items']}
        if len(values)!=source['count'] or set(values)!=set(request['scopes']):
            raise ValueError('exact-WB-roster-mismatch')
        values['TOTAL']=sum(values.values())
        changed=[]
        for component in after:
            scope=component['scope_key']
            if component['component_kind']!='WB' or scope not in values: continue
            number=values[scope]
            if number<0 or int(number)!=number: raise ValueError('invalid-WB-quantity')
            if component['state'] in ('exact','exact_zero'):
                if component['quantity']!=number: raise ValueError('nonempty-WB-value-outside-mask')
                continue
            component.update(state='exact' if number else 'exact_zero',quantity=int(number),
                source_revision=operation_id,source_digest=request['source_sha256'],source_watermark=day,
                provenance={'source':'official_historical_stocks_csv','business_date':day,'operation_id':operation_id,
                    'source_digest':request['source_sha256'],'warehouse_granularity_complete':source['warehouse_granularity_complete']})
            changed.append(scope)
        if set(changed)!=set(values):
            raise ValueError('WB-scope-is-not-entirely-missing')
        pointer=[dict(r) for r in conn.execute('SELECT * FROM '+FINALS+' WHERE business_date=? ORDER BY finalization_sequence',(day,))]
        arguments={'business_date':day,'capture_kind':'historical_backfill','formula_version':base['formula_version'],
            'facility_roster':json.loads(base['facility_roster_json']), 'source_manifest':{'operation_id':operation_id,
            'base_capture_id':base['capture_id'],'source':source,'source_digest':request['source_sha256']},
            'components':after,'captured_at':request['captured_at']}
        preview=preview_inventory_history_capture(**arguments)
        unchanged_before=[x for x in before if x['component_kind']!='WB' or x['scope_key'] not in changed]
        unchanged_after=[x for x in after if x['component_kind']!='WB' or x['scope_key'] not in changed]
        if digest(unchanged_before)!=digest(unchanged_after): raise ValueError('non-target-inventory-changed')
        return {'arguments':arguments,'capture_id':preview['capture_id'],'changed_scopes':changed,
            'publication_before':'unpublished' if initial_publication else 'finalized',
            'rollback_semantics':'published_all_missing_append_only' if initial_publication else 'prior_finalized_capture',
            'prestate_sha256':digest({'base':dict(base),'components':before,'pointer':pointer}),
            'before_capture':dict(base),'before_components':before,'before_pointer':pointer,
            'non_target_digest':digest(unchanged_before)}

    def preview(self, request, operation_id):
        runtime,db=self.target(request)
        backup=runtime/'evidence'/(operation_id+'.before.json')
        if backup.exists(): candidate=json.loads(backup.read_text())['candidate']
        else:
            with readonly(db) as conn: candidate=self.build(request,operation_id,conn)
        return {'operation_id':operation_id,'target':str(db),'scope':{'date':request['target_date'],'changed_WB_cells':len(candidate['changed_scopes'])},
            'prestate_sha256':candidate['prestate_sha256'],'candidate_sha256':digest(candidate),'candidate':candidate,
            'recovery':{'kind':'append-only prior immutable capture preserved','prior_capture_id':candidate['before_capture']['capture_id'],
                'publication_before':candidate['publication_before'],'semantics':candidate['rollback_semantics']}}

    def apply(self, request, operation_id, preview):
        runtime,db=self.target(request)
        with warehouse_functional_job_lock(runtime,blocking=False), warehouse_sync_lock(runtime,blocking=False):
            with sqlite3.connect(db,timeout=30) as conn:
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');self.target(request)
                candidate=self.build(request,operation_id,conn)
                if digest(candidate)!=preview['candidate_sha256']: raise ValueError('inventory-cas-drift')
                backup=runtime/'evidence'/(operation_id+'.before.json')
                if backup.exists(): raise ValueError('operation-already-attempted-use-readback')
                backup.parent.mkdir(exist_ok=True)
                private_json(backup,{'operation_id':operation_id,'candidate':candidate})
                result=append_inventory_history_capture(conn,**candidate['arguments'])
                if result['capture_id']!=candidate['capture_id']: raise ValueError('inventory-candidate-drift')
                append_inventory_history_finalization(conn,business_date=request['target_date'],capture_id=result['capture_id'],
                    finalization_identity=operation_id,finalized_at=request['captured_at'],provenance={'operation_id':operation_id,'source_digest':request['source_sha256']})
                conn.commit()
        return {'operation_id':operation_id,'disposition':'submitted'}

    def readback(self,request,operation_id):
        runtime,db=self.target(request);backup=runtime/'evidence'/(operation_id+'.before.json')
        if not backup.exists(): return {'operation_id':operation_id,'state':'not_submitted'}
        candidate=json.loads(backup.read_text())['candidate']
        with readonly(db) as conn:
            pointer=conn.execute('SELECT capture_id FROM '+FINALS+' WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1',(request['target_date'],)).fetchone()
            columns='scope_kind,scope_key,nm_id,component_kind,component_id,component_label,state,quantity,source_revision,source_digest,source_watermark,provenance_json'
            actual=[_stored_component(r) for r in conn.execute('SELECT '+columns+' FROM '+COMPONENTS+' WHERE capture_id=? ORDER BY scope_kind,scope_key,component_kind,component_id',(candidate['capture_id'],))]
        expected=sorted(candidate['arguments']['components'],key=lambda c:(c['scope_kind'],c['scope_key'],c['component_kind'],c['component_id']))
        okay=pointer and pointer[0]==candidate['capture_id'] and digest(actual)==digest(expected)
        return {'operation_id':operation_id,'state':'applied' if okay else 'ambiguous','capture_id':candidate['capture_id'],
                'verified_WB_cells':len(candidate['changed_scopes']) if okay else 0,'non_target_digest':candidate['non_target_digest']}

    def rollback(self,request,operation_id):
        runtime,db=self.target(request)
        candidate=json.loads((runtime/'evidence'/(operation_id+'.before.json')).read_text())['candidate']
        with warehouse_functional_job_lock(runtime,blocking=False), warehouse_sync_lock(runtime,blocking=False):
            with sqlite3.connect(db) as conn:
                conn.execute('BEGIN IMMEDIATE')
                pointer=conn.execute('SELECT capture_id FROM '+FINALS+' WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1',(request['target_date'],)).fetchone()
                if not pointer or pointer[0]!=candidate['capture_id']: raise ValueError('inventory-rollback-cas-drift')
                append_inventory_history_finalization(conn,business_date=request['target_date'],capture_id=candidate['before_capture']['capture_id'],
                    finalization_identity=operation_id+'-rollback',finalized_at=request['captured_at'],provenance={'rollback_of':operation_id})
                conn.commit()
        return {'operation_id':operation_id,'restored_capture_id':candidate['before_capture']['capture_id']}
