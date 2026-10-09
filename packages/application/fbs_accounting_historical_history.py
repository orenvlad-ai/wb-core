"""Exact source-owned History acknowledgment for a published receipt revision.

This is not a ClosedBacklog admission and cannot enroll arbitrary dates. The
existing authenticated History supervisor owns compilation and the one-use
acknowledgment. Durable evidence lives with the actual publication intent.
"""
from __future__ import annotations
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from packages.application import ready_publication as ready
from packages.application import fbs_accounting_runtime as accounting
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_accounting_historical_publication import CONTRACT
from packages.application.fbs_accounting_historical_stages import require


class HistoricalReceipt:
    def __init__(self, runtime, operation_id, attempt_id):
        self.runtime = runtime
        self.operation_id, self.attempt_id = operation_id, attempt_id
        self.manifest = self._read()[0]
        require(self.manifest['contract'] == CONTRACT, 'historical_history_wrong_publication_kind')
        self.dates = tuple(day for day in self.manifest['plan']['scope']['dates'] if day < self.manifest['plan']['scope']['date_to'])
        require(0 < len(self.dates) <= 366 and self.dates == tuple(sorted(set(self.dates))), 'historical_history_scope_invalid')
        for day in self.dates:
            require(datetime.strptime(day, '%Y-%m-%d').date().isoformat() == day, 'historical_history_date_invalid')

    def _read(self):
        with ready.readonly(self.runtime.db_path) as conn:
            size=conn.execute('SELECT length(inputs_json),length(diagnostics_json) FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(self.operation_id,self.attempt_id)).fetchone()
            require(size and size[0]<=160*1024**2 and size[1]<=32*1024**2,'historical_history_publication_size_limit')
            row = conn.execute('SELECT kind,state,inputs_json,book_version,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?', (self.operation_id,self.attempt_id)).fetchone()
        require(row and row['kind']==CONTRACT and row['state']=='complete', 'historical_history_publication_not_complete')
        manifest = json.loads(row['inputs_json'])
        require(manifest['manifest_digest'] == fingerprint({k:v for k,v in manifest.items() if k not in {'manifest_digest','prepare_seconds'}})
                and manifest['after_book'] == row['book_version'], 'historical_history_manifest_corrupt')
        return manifest, json.loads(row['diagnostics_json'])

    def freeze_scope(self,now):
        from packages.business_time import current_business_date_iso
        capture_day=self.manifest['plan']['scope']['date_to']
        self.dates=tuple(d for d in self.manifest['plan']['scope']['dates'] if d<capture_day or current_business_date_iso(now)>capture_day)
        return self

    def binding(self):
        manifest,_ = self._read()
        require(manifest == self.manifest, 'historical_history_receipt_changed')
        return dict(operation_id=self.operation_id,attempt_id=self.attempt_id,digest=manifest['manifest_digest'],dates=list(self.dates))

    def publication_dates(self):
        from packages.application.business_data_heavy_admission import require_heavy_owner
        require(require_heavy_owner(self.runtime.runtime_dir).operation=='cycle','historical_history_cycle_owner_required')
        self.binding()
        return self.dates

    def validate_sources_readonly(self, *, now):
        """Native selected ready preference and immutable dated book sources.

        A final current ready refresh may legitimately change envelope metadata;
        the exact selected dated binding and numerical inputs must still carry
        this revision. A later book may retain these immutable dates unchanged.
        """
        from packages.application.operator_warehouse_documents import TABLE,assert_source
        from packages.application.ff_pool_documents import REQUESTS_TABLE
        from packages.application.web_vitrina_window_v3 import _ReadyHeader,_select_bindings
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
        from packages.business_time import default_business_as_of_date
        manifest = self.manifest
        current,version = accounting.load(self.runtime.runtime_dir)
        expected = manifest['candidate']['candidate_book']
        capture_day=manifest['plan']['scope']['date_to']
        fixed_dates=[d for d in self.dates if d<capture_day]
        from packages.business_time import current_business_date_iso
        require(all(d<capture_day or current_business_date_iso(now)>capture_day for d in self.dates),'historical_history_future_closed_enrollment')
        for field in ('wb_days','retained_days','shared_days','presentations'):
            require(all(current[field].get(day)==expected[field][day] for day in fixed_dates),'historical_history_book_dates_changed')
        if capture_day in self.dates:
            period=current['state']['periods'].get(capture_day,{})
            require(period.get('status')=='closed' and period.get('snapshot',{}).get('date')==capture_day
                and all(capture_day in current[field] for field in ('wb_days','retained_days','shared_days','presentations')),
                'historical_history_rollover_date_unpublished')
            # New legitimate open-day facts may change D before its ordinary
            # close. The exact closed D image is now the immutable authority.
            for field in ('wb_days','retained_days','shared_days','presentations'):
                expected=deepcopy(expected);expected[field][capture_day]=current[field][capture_day]
        consumed=dict(current['state']['baseline'].get('absorbed_documents',{}))
        for period in current['state']['periods'].values():consumed.update(period.get('applied_documents',{}))
        require(all(consumed.get(identity)==value['fingerprint'] for identity,value in manifest['source_confirmation'].items()),'historical_history_source_not_absorbed')
        proof = {}
        with ready.readonly(self.runtime.db_path) as conn:
            ready.check_pinned_authority(conn,manifest['authority'])
            for identity,confirmation in manifest['operator_confirmation'].items():
                actual = conn.execute(f'SELECT source_json,source_digest,accepted_at,actor FROM {TABLE} WHERE request_id=?',(identity,)).fetchone()
                request = conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(identity,)).fetchone()
                require(actual and dict(actual)==confirmation and fingerprint(assert_source(conn,request))==confirmation['source_digest'],'historical_history_source_changed')
            count,size=conn.execute('SELECT count(*),coalesce(sum(length(plan_json)),0) FROM sheet_vitrina_v1_ready_snapshots').fetchone()
            require(count<=10000 and size<=160*1024**2,'historical_history_ready_size_limit')
            rows = conn.execute('SELECT s.*,r.revision FROM sheet_vitrina_v1_ready_snapshots s JOIN sheet_vitrina_v1_ready_revisions r USING(bundle_version,as_of_date) ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,s.bundle_version DESC').fetchall()
            require(len(rows)<=10000, 'historical_history_ready_limit')
            plans = {(r['bundle_version'],r['as_of_date']):_deserialize_sheet_vitrina_plan(r['plan_json']) for r in rows}
            headers = [_ReadyHeader(r['bundle_version'],r['as_of_date'],r['snapshot_id'],r['activated_at'],r['refreshed_at'],tuple(plans[(r['bundle_version'],r['as_of_date'])].date_columns),canonical(plans[(r['bundle_version'],r['as_of_date'])].metadata.get('fbs_accounting_bindings',{})),r['revision']) for r in rows]
            bundle = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
            bindings,_ = _select_bindings(conn,headers,list(self.dates),bundle,default_business_as_of_date(now))
            for binding in bindings:
                require(binding.source_key is not None,'historical_history_ready_date_missing',day=binding.date)
                plan = plans[binding.source_key]
                bound = plan.metadata.get('fbs_accounting_bindings',{}).get(binding.date,{})
                bound_book,bound_version = accounting.load(self.runtime.runtime_dir,version=bound.get('book_version')) if bound.get('book_version') else (None,None)
                require(bound_book and all(bound_book[field].get(binding.date)==expected[field][binding.date] for field in ('wb_days','retained_days','shared_days','presentations')),'historical_history_ready_book_binding_changed',day=binding.date)
                proof[binding.date] = dict(ready_key=list(binding.source_key),book_version=bound_version,ready_digest=ready.digest(next(r['plan_json'] for r in rows if (r['bundle_version'],r['as_of_date'])==binding.source_key)),functional_version=expected['wb_days'][binding.date]['version_id'])
        require(self._read()[0]==manifest, 'historical_history_receipt_changed')
        return dict(binding=self.binding(),active_book=version,dated=proof)

    def validate_native_readonly(self, *, adapter, store, now):
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.owned_history_native_ack import _object
        require(type(adapter) is LiveNativeAdapter and type(store) is HistoryStore
                and adapter.db_path==Path(self.runtime.db_path).resolve() and adapter.runtime_dir==Path(self.runtime.runtime_dir).resolve(),'historical_history_native_context_changed')
        before=self.validate_sources_readonly(now=now)
        vector=adapter.capture();fence=adapter.fence;pointer=store._current()
        require(pointer is not None,'historical_history_current_missing')
        edition=store.edition();proofs=store.day_proofs(edition)
        native={}
        for day in self.dates:
            require(day in edition['days'] and proofs.get(day)==dict(epoch=vector['epoch'],token=vector['dates'].get(day)), 'historical_history_native_date_pending',day=day)
            _object(store,day,edition['days'][day],store.day_catalogs(edition)[day],time.monotonic()+30)
            native[day]=dict(edition_id=pointer['current'],object_digest=edition['days'][day],**proofs[day],**before['dated'][day])
        require(self.validate_sources_readonly(now=now)==before and adapter.capture()==vector and adapter.fence==fence and store._current()==pointer,'historical_history_changed_during_ack')
        return dict(receipt_digest=self.manifest['manifest_digest'],current=pointer,vector=vector,fence=fence,native=native)

    def _acknowledge_verified_native(self,proof):
        from packages.application.owned_history_native_ack import consume_historical_history_ack
        native=consume_historical_history_ack(proof,self.runtime.runtime_dir,self.manifest['manifest_digest'],self.dates)
        # The live supervisor still owns every history descriptor. A short CAS
        # appends evidence to this real publication, never changes its inputs.
        with closing(sqlite3.connect(Path(self.runtime.db_path).as_uri()+'?mode=rw',uri=True,timeout=0)) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=? AND state=\'complete\'',(self.operation_id,self.attempt_id)).fetchone()
            require(row and json.loads(row[0])==self.manifest,'historical_history_receipt_changed')
            diagnostics=json.loads(row[1]);diagnostics['historical_history_ack']=dict(manifest_digest=self.manifest['manifest_digest'],native={d:dict(v) for d,v in native.items()})
            require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',(canonical(diagnostics),self.operation_id,self.attempt_id,row[1])).rowcount==1,'historical_history_ack_cas_failed')


def pending_receipt(runtime):
    """One real published contour; no synthetic or unselected date enrollment."""
    if not hasattr(runtime,'db_path'):return None  # Existing cycle test doubles carry no database.
    with ready.readonly(runtime.db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_vitrina_v1_ready_publications'").fetchone():return None
        rows=conn.execute('SELECT operation_id,attempt_id,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE kind=? AND state=\'complete\' ORDER BY created_at,operation_id,attempt_id',(CONTRACT,)).fetchall()
    require(len(rows)<=10000,'historical_history_publication_limit')
    selected=[r for r in rows if not json.loads(r['diagnostics_json']).get('historical_operator_complete')]
    require(len(selected)<=1,'historical_history_multiple_unfinished_publications')
    return HistoricalReceipt(runtime,selected[0]['operation_id'],selected[0]['attempt_id']) if selected else None


def restore_revision_rows(rows, *, runtime, plan, inventory_history, business_date):
    """Native full physical warehouse rows do not own revised management cost.

    Only exact ready-bound historical editions with a native typed quantity
    capture may restore the admitted presentation. The ordinary source overlay
    and its guard still own every other date/version.
    """
    from packages.application.fbs_accounting_runtime import ActiveInventorySnapshot
    from packages.application.inventory_quantity import resolve_plan_quantities
    for day,binding in dict(plan.metadata or {}).get('fbs_accounting_bindings',{}).items():
        if day>=business_date:continue
        book,version=accounting.load(runtime.runtime_dir,version=binding['book_version'])
        edition=book.get('historical_revision',{}).get('native_stage_editions',{}).get(day)
        if not edition:continue
        require(book['wb_days'][day]['version_id']==edition or day==book['historical_revision']['date_to'],
            'historical_management_stage_binding_changed',day=day)
        operands=resolve_plan_quantities(plan,day=day,runtime_dir=runtime.runtime_dir,require_closed=True)
        require(operands is not None,'historical_management_quantity_binding_missing',day=day)
        dated=inventory_history.get('dates',{}).get(day,{})
        require(dated.get('finalization_id') or dated.get('accepted_publication'),'historical_management_quantity_publication_missing',day=day)
        with ready.readonly(runtime.db_path) as conn:
            from packages.application.sheet_vitrina_v1_inventory_history import CAPTURES_TABLE
            publication=conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE kind=? AND state='complete' AND json_extract(inputs_json,?)=? LIMIT 1",
                (CONTRACT,'$.candidate.editions.\"'+day+'\".version_id',edition)).fetchone()
            require(publication is not None,'historical_management_native_publication_missing',day=day)
            capture=conn.execute(f'SELECT source_manifest_json FROM {CAPTURES_TABLE} WHERE capture_id=?',(dated.get('capture_id',''),)).fetchone()
            require(capture and json.loads(capture[0])['source_refs']['presentation_digest']==fingerprint(book['presentations'][day]),
                'historical_management_quantity_presentation_changed',day=day)
        payload=deepcopy(book['presentations'][day]);payload['accounting_effective_date']=book['effective_date']
        snapshot=ActiveInventorySnapshot.__new__(ActiveInventorySnapshot)
        object.__setattr__(snapshot,'_json',canonical(payload))
        rows=snapshot.apply_rows(rows,business_date=day)
    return rows
