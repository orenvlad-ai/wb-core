"""Exact source-owned History acknowledgment for a published receipt revision.

This is not a ClosedBacklog admission and cannot enroll arbitrary dates. The
existing authenticated History supervisor owns compilation and the one-use
acknowledgment. Durable evidence lives with the actual publication intent.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

from packages.application import fbs_accounting_historical_sqlite as historical_sqlite
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_runtime as accounting
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_accounting_historical_publication import CONTRACT,PUBLICATION_CONTRACTS,PUBLICATION_SQL_PLACEHOLDERS,FBO_CONTRACT,TEMPORARY_CONTRACT,decode_manifest,publication_limits
from packages.application.fbs_accounting_historical_stages import require

MAX_READY_HEADERS = 10000
MAX_READY_HEADER_BYTES = 8 * 1024**2
MAX_READY_SOURCE_BYTES = 160 * 1024**2
_CHUNK = 64 * 1024


def _stream_fingerprint(value):
    """The native canonical JSON digest, without a complete JSON/UTF-8 copy.

    Receipt JSON has string dictionary keys. Escape strings in bounded pieces
    too: an individual retained Ready image can itself be tens of megabytes.
    """
    hashed = hashlib.sha256()
    def emit(text):
        for offset in range(0, len(text), _CHUNK):
            hashed.update(text[offset:offset + _CHUNK].encode())
    def visit(item):
        if isinstance(item, str):
            emit('"')
            for offset in range(0, len(item), _CHUNK):
                emit(json.encoder.encode_basestring(item[offset:offset + _CHUNK])[1:-1])
            emit('"')
        elif isinstance(item, dict):
            emit('{')
            for index, key in enumerate(sorted(item)):
                if not isinstance(key, str):
                    raise TypeError('receipt JSON keys must be strings')
                if index: emit(',')
                visit(key); emit(':'); visit(item[key])
            emit('}')
        elif isinstance(item, (list, tuple)):
            emit('[')
            for index, child in enumerate(item):
                if index: emit(',')
                visit(child)
            emit(']')
        else:
            emit(canonical(item))
    visit(value)
    return 'sha256:' + hashed.hexdigest()


def _selected_ready_sources(conn, *, dates, bundle, now):
    """Keep native global-bundle preference; load only selected dated bodies."""
    from packages.application.web_vitrina_window_v3 import _ReadyHeader, _select_bindings, has_complete_recovery
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    from packages.application.sheet_vitrina_v1 import _require_date_columns_or_infer
    from packages.business_time import default_business_as_of_date
    metadata = f'''SELECT s.bundle_version,s.as_of_date,s.snapshot_id,s.activated_at,s.refreshed_at,r.revision,
        json_extract(s.plan_json,'$.date_columns') dates_json,
        (SELECT json_extract(value,'$.header') FROM json_each(s.plan_json,'$.sheets')
         WHERE json_extract(value,'$.sheet_name')='DATA_VITRINA' LIMIT 1) header_json,
        json_extract(s.plan_json,'$.metadata.fbs_accounting_bindings') bindings_json
        FROM {ready.TABLE} s JOIN sheet_vitrina_v1_ready_revisions r USING(bundle_version,as_of_date)'''
    count,size = conn.execute(f'''SELECT count(*),coalesce(sum(
        length(CAST(bundle_version||as_of_date||snapshot_id||activated_at||refreshed_at AS BLOB))+
        length(CAST(coalesce(dates_json,'') AS BLOB))+length(CAST(coalesce(header_json,'') AS BLOB))+
        length(CAST(coalesce(bindings_json,'') AS BLOB))),0)
        FROM ({metadata})''').fetchone()
    require(count <= MAX_READY_HEADERS and size <= MAX_READY_HEADER_BYTES, 'historical_history_ready_header_size_limit')
    # Active bundle participates only in the native default preference. Exact
    # and covering dates retain the original global order across all bundles.
    headers=[]
    for row in conn.execute(metadata+' ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,s.bundle_version DESC'):
        header=json.loads(row['header_json'] or '[]')
        columns=_require_date_columns_or_infer({'date_columns':json.loads(row['dates_json']) if row['dates_json'] is not None else None},header)
        headers.append(_ReadyHeader(row['bundle_version'],row['as_of_date'],row['snapshot_id'],row['activated_at'],row['refreshed_at'],
            tuple(columns),row['bindings_json'] or '{}',row['revision']))
    recovery_cache={};recovery_bytes=0
    def recovery(connection,header,day):
        nonlocal recovery_bytes
        if header.key not in recovery_cache:
            field="json_extract(plan_json,'$.metadata.server_cell_presentation')"
            size=connection.execute(f'SELECT length(CAST({field} AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',header.key).fetchone()
            recovery_bytes+=(size[0] or 0) if size else 0
            require(recovery_bytes<=MAX_READY_SOURCE_BYTES,'historical_history_ready_recovery_size_limit')
            row=connection.execute(f'SELECT {field} FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',header.key).fetchone()
            try:recovery_cache[header.key]=json.loads(row[0] or '{}') if row else {}
            except json.JSONDecodeError:recovery_cache[header.key]={}
        return has_complete_recovery({'server_cell_presentation':recovery_cache[header.key]},day)
    bindings,_=_select_bindings(conn,headers,list(dates),bundle,default_business_as_of_date(now),recovery_check=recovery)
    selected=sorted({b.source_key for b in bindings if b.source_key is not None})
    size=0
    for key in selected:
        row=conn.execute(f'SELECT length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',key).fetchone()
        require(row is not None,'historical_history_ready_date_missing')
        size+=row[0]
        require(size<=MAX_READY_SOURCE_BYTES,'historical_history_ready_size_limit')
    texts={key:conn.execute(f'SELECT plan_json FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',key).fetchone()[0] for key in selected}
    return bindings,{key:_deserialize_sheet_vitrina_plan(value) for key,value in texts.items()},texts


class HistoricalReceipt:
    def __init__(self, runtime, operation_id, attempt_id):
        self.runtime = runtime
        self.operation_id, self.attempt_id = operation_id, attempt_id
        manifest, _ = self._read()
        scope = manifest['plan']['scope']
        book = manifest['candidate']['candidate_book']
        # History consumes no Ready before/after images, stage copies, or open
        # book suffix. The immutable stored-byte pin still covers all of them.
        self._history = dict(contract=manifest['contract'], manifest_digest=manifest['manifest_digest'],
            scope=scope, authority=manifest['authority'],
            book_days={field: {day: book[field][day] for day in scope['dates'] if day in book[field]}
                for field in ('wb_days', 'retained_days', 'shared_days', 'presentations')},
            source_confirmation={key: value['fingerprint'] for key, value in manifest['source_confirmation'].items()},
            operator_confirmation=manifest['operator_confirmation'])
        self._history_digest = _stream_fingerprint(self._history)
        self.dates = tuple(day for day in scope['dates'] if day < scope['date_to'])
        require(0 < len(self.dates) <= 366 and self.dates == tuple(sorted(set(self.dates))), 'historical_history_scope_invalid')
        for day in self.dates:
            require(datetime.strptime(day, '%Y-%m-%d').date().isoformat() == day, 'historical_history_date_invalid')

    @property
    def manifest(self):
        """Compatibility for finalizers; the caller owns this transient image."""
        return self._read()[0]

    @contextmanager
    def _snapshot(self):
        with ready.readonly(self.runtime.db_path) as conn:
            owned = not conn.in_transaction
            if owned: conn.execute('BEGIN')
            try: yield conn
            finally:
                if owned: conn.rollback()

    def _observe(self, conn):
        row = conn.execute('SELECT rowid,kind,state,book_version FROM sheet_vitrina_v1_ready_publications '
            'WHERE operation_id=? AND attempt_id=?', (self.operation_id,self.attempt_id)).fetchone()
        require(row is not None, 'historical_history_publication_not_complete')
        row = tuple(row)
        require(row[1] in PUBLICATION_CONTRACTS and row[2]=='complete', 'historical_history_publication_not_complete')
        limit, _ = publication_limits(row[1])
        with conn.blobopen('sheet_vitrina_v1_ready_publications', 'diagnostics_json', row[0], readonly=True) as blob:
            require(len(blob) <= 32*1024**2, 'historical_history_publication_size_limit')
        hashed = hashlib.sha256()
        with conn.blobopen('sheet_vitrina_v1_ready_publications', 'inputs_json', row[0], readonly=True) as blob:
            size = len(blob)
            require(size <= limit, 'historical_history_publication_size_limit')
            while chunk := blob.read(_CHUNK): hashed.update(chunk)
        observed = (*row, size, hashed.hexdigest())
        require(not hasattr(self, '_stored') or observed == self._stored, 'historical_history_receipt_changed')
        return observed

    def _diagnostics(self, conn, rowid):
        with conn.blobopen('sheet_vitrina_v1_ready_publications', 'diagnostics_json', rowid, readonly=True) as blob:
            require(len(blob) <= 32*1024**2, 'historical_history_publication_size_limit')
        return json.loads(conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE rowid=?', (rowid,)).fetchone()[0])

    def _load(self, conn):
        observed = self._observe(conn)
        encoded = conn.execute('SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE rowid=?', (observed[0],)).fetchone()[0]
        manifest = decode_manifest(encoded, kind=observed[1])
        del encoded  # Do not retain the stored TEXT across expanded hashing.
        require(manifest['contract']==observed[1], 'historical_history_publication_kind_changed')
        require(manifest['manifest_digest'] == _stream_fingerprint({k:v for k,v in manifest.items() if k not in {'manifest_digest','prepare_seconds'}})
                and manifest['after_book'] == observed[3], 'historical_history_manifest_corrupt')
        self._stored = observed
        return manifest

    def _read(self):
        # Full compatibility read is deliberately not cached in the receipt.
        with self._snapshot() as conn:
            manifest = self._load(conn)
            return manifest, self._diagnostics(conn, self._stored[0])

    def _read_history(self, conn=None):
        if conn is None:
            with self._snapshot() as source: return self._read_history(source)
        observed = self._observe(conn)
        require(_stream_fingerprint(self._history)==self._history_digest, 'historical_history_receipt_changed')
        return self._history, self._diagnostics(conn, observed[0])

    def _validate_typed_source(self, conn):
        # The existing pinned native validators also check the entire TEMP
        # draft numeric admission. Give them the genuine full manifest, then
        # release it before loading current book and selected Ready operands.
        contract = self._history['contract']
        if contract == TEMPORARY_CONTRACT:
            from packages.application.fbs_temporary_allocation_action import validate_source_receipt
        elif contract == FBO_CONTRACT:
            from packages.application.fbs_historical_fbo_publication import validate_source_receipt
        else:
            return
        validate_source_receipt(conn, manifest=self._load(conn))

    def freeze_scope(self,now):
        from packages.business_time import current_business_date_iso
        scope=self._history['scope'];capture_day=scope['date_to']
        self.dates=tuple(d for d in scope['dates'] if d<capture_day or current_business_date_iso(now)>capture_day)
        return self

    def binding(self):
        manifest,_ = self._read_history()
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
        manifest, _ = self._read_history()
        with self._snapshot() as conn:
            self._validate_typed_source(conn)
            version,proof=self._validate_sources_snapshot(conn,manifest=manifest,now=now)
        self._read_history()
        return dict(binding=self.binding(),active_book=version,dated=proof)

    def _validate_sources_snapshot(self, conn, *, manifest, now):
        from packages.application.operator_warehouse_documents import TABLE,assert_source
        from packages.application.ff_pool_documents import REQUESTS_TABLE
        current,version = accounting.load(self.runtime.runtime_dir)
        expected = manifest['book_days']
        capture_day=manifest['scope']['date_to']
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
            expected={field:{**values,capture_day:current[field][capture_day]} for field,values in expected.items()}
        consumed=dict(current['state']['baseline'].get('absorbed_documents',{}))
        for period in current['state']['periods'].values():consumed.update(period.get('applied_documents',{}))
        require(all(consumed.get(identity)==value for identity,value in manifest['source_confirmation'].items()),'historical_history_source_not_absorbed')
        proof = {}
        ready.check_pinned_authority(conn,manifest['authority'])
        for identity,confirmation in manifest['operator_confirmation'].items():
            actual = conn.execute(f'SELECT source_json,source_digest,accepted_at,actor FROM {TABLE} WHERE request_id=?',(identity,)).fetchone()
            request = conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(identity,)).fetchone()
            require(actual and dict(actual)==confirmation and fingerprint(assert_source(conn,request))==confirmation['source_digest'],'historical_history_source_changed')
        bundle = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
        bindings,plans,texts = _selected_ready_sources(conn,dates=self.dates,bundle=bundle,now=now)
        for binding in bindings:
            require(binding.source_key is not None,'historical_history_ready_date_missing',day=binding.date)
            plan = plans[binding.source_key]
            bound = plan.metadata.get('fbs_accounting_bindings',{}).get(binding.date,{})
            bound_book,bound_version = accounting.load(self.runtime.runtime_dir,version=bound.get('book_version')) if bound.get('book_version') else (None,None)
            require(bound_book and all(bound_book[field].get(binding.date)==expected[field][binding.date] for field in ('wb_days','retained_days','shared_days','presentations')),'historical_history_ready_book_binding_changed',day=binding.date)
            proof[binding.date] = dict(ready_key=list(binding.source_key),book_version=bound_version,ready_digest=ready.digest(texts[binding.source_key]),functional_version=expected['wb_days'][binding.date]['version_id'])
        return version,proof

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
        return dict(receipt_digest=self._history['manifest_digest'],current=pointer,vector=vector,fence=fence,native=native)

    def _acknowledge_verified_native(self,proof):
        from packages.application.owned_history_native_ack import consume_historical_history_ack
        native=consume_historical_history_ack(proof,self.runtime.runtime_dir,self._history['manifest_digest'],self.dates)
        # The live supervisor still owns every history descriptor. A short CAS
        # appends evidence to this real publication, never changes its inputs.
        with historical_sqlite.connection(self.runtime.db_path,phase_name='history_ack_connection') as conn, historical_sqlite.transaction(conn,db_path=self.runtime.db_path,phase_name='history_ack'):
            manifest,diagnostics=self._read_history(conn)
            raw=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?', (self.operation_id,self.attempt_id)).fetchone()[0]
            diagnostics['historical_history_ack']=dict(manifest_digest=manifest['manifest_digest'],native={d:dict(v) for d,v in native.items()})
            require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',(canonical(diagnostics),self.operation_id,self.attempt_id,raw)).rowcount==1,'historical_history_ack_cas_failed')


def pending_receipt(runtime):
    """One real published contour; no synthetic or unselected date enrollment."""
    if not hasattr(runtime,'db_path'):return None  # Existing cycle test doubles carry no database.
    with ready.readonly(runtime.db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_vitrina_v1_ready_publications'").fetchone():return None
        rows=conn.execute(f'SELECT operation_id,attempt_id,kind,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE kind IN ({PUBLICATION_SQL_PLACEHOLDERS}) AND state=\'complete\' ORDER BY created_at,operation_id,attempt_id',PUBLICATION_CONTRACTS).fetchall()
    require(len(rows)<=10000,'historical_history_publication_limit')
    selected=[r for r in rows if not json.loads(r['diagnostics_json']).get('temporary_allocation_scope_complete' if r['kind']==TEMPORARY_CONTRACT else 'historical_fbo_scope_complete' if r['kind']==FBO_CONTRACT else 'historical_operator_complete')]
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
            publication=conn.execute(f"SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE kind IN ({PUBLICATION_SQL_PLACEHOLDERS}) AND state='complete' AND json_extract(inputs_json,?)=? LIMIT 1",
                (*PUBLICATION_CONTRACTS,'$.candidate.editions.\"'+day+'\".version_id',edition)).fetchone()
            require(publication is not None,'historical_management_native_publication_missing',day=day)
            capture=conn.execute(f'SELECT source_manifest_json FROM {CAPTURES_TABLE} WHERE capture_id=?',(dated.get('capture_id',''),)).fetchone()
            require(capture and json.loads(capture[0])['source_refs']['presentation_digest']==fingerprint(book['presentations'][day]),
                'historical_management_quantity_presentation_changed',day=day)
        payload=deepcopy(book['presentations'][day]);payload['accounting_effective_date']=book['effective_date']
        snapshot=ActiveInventorySnapshot.__new__(ActiveInventorySnapshot)
        object.__setattr__(snapshot,'_json',canonical(payload))
        rows=snapshot.apply_rows(rows,business_date=day)
    return rows
