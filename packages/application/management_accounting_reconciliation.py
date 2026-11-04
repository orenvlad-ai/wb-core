"""Bounded Ready-only recovery from already closed, verified accounting views."""
from copy import deepcopy
from datetime import date,datetime,timezone
import json
from pathlib import Path

from packages.application import fbs_accounting_runtime as accounting
from packages.application.fbs_accounting_degraded import reconcile_recovered_accounting,METADATA_KEY
from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
from packages.application.web_vitrina_management_history import digest,data_sheet,dated_parameters,recalculate_dated_proxy,SOURCE as PROXY_SOURCE
from packages.business_time import current_business_date_iso

CONTRACT='closed_accounting_ready_reconciliation_v1'
REASON='incomplete_period_cannot_close'


def require(ok,reason):
    if not ok:raise ValueError('accounting-ready-reconcile:'+reason)


def capture(runtime,conn,*,dates,ready_dates):
    """Exact outer Ready keys, never all snapshots overlapping a date column."""
    if not conn.in_transaction:conn.execute('BEGIN')
    from packages.application.ready_publication import capture_authority,check_pinned_authority
    authority=capture_authority(runtime);check_pinned_authority(conn,authority)
    days=sorted(set(dates));outer=sorted(set(ready_dates));today=current_business_date_iso(datetime.now(timezone.utc))
    require(0<len(days)<=31 and 0<len(outer)<=3,'bounded-date-scope')
    require(all(date.fromisoformat(d).isoformat()==d and d<today for d in days),'past-dates-only')
    require(set(outer)==set(days),'exact-own-date-scope-required')
    active=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
    require(active is not None,'active-bundle-missing');bundle=active[0]
    records=[]
    for day in outer:
        row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(bundle,day)).fetchone()
        require(row is not None,'exact-ready-missing:'+day);records.append(dict(row))
    require(sum(len(r['plan_json'].encode()) for r in records)<=64*1024**2,'ready-byte-budget')
    book,version=accounting.load(runtime)
    require(book and book.get('active') and not book.get('publication_error'),'healthy-book-required')
    presentations={}
    for day in days:
        period=book['state']['periods'].get(day);view=book['presentations'].get(day)
        require(period and period.get('status')=='closed' and period.get('quality')=='closed_snapshot_wac'
            and not period.get('pending_documents') and view and view.get('quality') not in {'incomplete','unavailable'}
            and view.get('cost_status')=='closed' and not view.get('pending_documents')
            and book['shared_days'].get(day,{}).get('quality')=='complete','closed-complete-book-day:'+day)
        presentations[day]=dict(version_id=view['version_id'],sha256=digest(view))
    source=dict(contract=CONTRACT,authority=authority,snapshot_id=version,book_version=version,bundle_version=bundle,dates=days,
        presentations=presentations,ready_snapshots=[dict(as_of_date=r['as_of_date'],snapshot_id=r['snapshot_id'],plan_sha256=digest(r['plan_json'])) for r in records])
    return source,records,book


def _metadata_projection(raw,*,book,version,runtime,record,days,parameters,operation_id):
    quality=raw.get('metadata',{}).get(METADATA_KEY)
    require(quality and quality.get('reason')==REASON,'recognized-suppression-required')
    own=record['as_of_date']
    require(own in days and own in raw.get('date_columns',[]) and own in quality.get('dates',[]),'own-date-suppression-required')
    selected=[own]
    binding=raw.get('metadata',{}).get('fbs_accounting_bindings',{}).get(own)
    require(binding and binding.get('presentation_version')==book['presentations'][own]['version_id']
        and binding.get('source')==accounting.SOURCE
        and binding.get('ready_target')==dict(bundle_version=record['bundle_version'],as_of_date=own),
        'saved-presentation-binding-changed:'+own)
    partial={k:deepcopy(book[k]) for k in ('active','effective_date')}
    partial['presentations']={d:book['presentations'][d] for d in selected}
    plan=reconcile_recovered_accounting(_deserialize_sheet_vitrina_plan(json.dumps(raw)),book=partial)
    plan=accounting.materialize(plan,runtime_dir=runtime,now=datetime.now(timezone.utc),book=partial,
        book_version=version,ready_target=dict(bundle_version=record['bundle_version'],as_of_date=record['as_of_date']))
    projected=json.loads(_serialize_sheet_vitrina_plan(plan))
    for day in selected:
        projected=recalculate_dated_proxy(projected,day=day,parameters=parameters[day],operation_id=operation_id)['plan']
    # This recovery releases stale visibility only. It may not fill, erase,
    # or reprice a DATA cell, even if the ordinary producer could do so.
    require(data_sheet(projected)['rows']==data_sheet(raw)['rows'],'numeric-data-change')
    before=raw.get('metadata',{});after=projected.get('metadata',{})
    for field in ('fbs_accounting_bindings','fbs_accounting_targets'):
        if field in after:
            after[field]={**{d:v for d,v in before.get(field,{}).items() if d not in selected},
                          **{d:v for d,v in after[field].items() if d in selected}}
    allowed={METADATA_KEY,'fbs_accounting','fbs_accounting_bindings','server_cell_presentation'}
    require({k:v for k,v in before.items() if k not in allowed}=={k:v for k,v in after.items() if k not in allowed},'unrelated-metadata-change')
    old_cells=before.get('server_cell_presentation',{});new_cells=after.get('server_cell_presentation',{})
    changed=[]
    for key in sorted(set(old_cells)|set(new_cells)):
        for day in sorted(set(old_cells.get(key,{}))|set(new_cells.get(key,{}))):
            old=old_cells.get(key,{}).get(day);new=new_cells.get(key,{}).get(day)
            if old==new:continue
            require(day in selected,'unselected-cell-change')
            require((old or {}).get('reason_code')==REASON or ((new or {}).get('source')==accounting.SOURCE
                and (old is None or old.get('source')==accounting.SOURCE)),'unowned-cell-change')
            changed.append(dict(row_id=key,date=day,before=old,after=new))
    # Preserve exact DATA and all untouched serialization fields, not merely
    # numeric equivalence (native materialization may normalize 1 to 1.0).
    output=deepcopy(raw);output['metadata']=after
    old_sheets={s['sheet_name']:s for s in raw['sheets']}
    for sheet in projected['sheets']:
        name=sheet['sheet_name']
        if name in ('DATA_VITRINA','STATUS'):continue
        require(sheet==old_sheets[name],'unrelated-sheet-change')
    old_status=old_sheets.get('STATUS');new_status=next((s for s in projected['sheets'] if s['sheet_name']=='STATUS'),None)
    if old_status!=new_status:
        require(old_status is not None and new_status is not None,'status-shape-change')
        expected=[r for r in old_status['rows'] if not r or str(r[0])!='fbs_snapshot_accounting']
        require(new_status['rows']==expected and METADATA_KEY not in after,'unrelated-status-change')
        require({k:v for k,v in old_status.items() if k not in {'rows','row_count','write_rect'}}==
                {k:v for k,v in new_status.items() if k not in {'rows','row_count','write_rect'}},'status-format-change')
        output['sheets']=[new_status if s['sheet_name']=='STATUS' else s for s in output['sheets']]
    require(data_sheet(output)==data_sheet(raw),'data-byte-shape-change')
    return output,changed


def build(request,operation_id,conn):
    require(not any(request.get(k) for k in ('recalculate_proxy_only','last_known_as_of','current_only','complete_day_sources')),'mixed-recovery-mode')
    source=request['source'];require(digest(source)==request['source_sha256'],'frozen-source-digest')
    current,records,book=capture(Path(request['runtime_dir']),conn,dates=request['dates'],ready_dates=[r['as_of_date'] for r in source['ready_snapshots']])
    require(source==current,'source-compare-and-swap')
    parameters={day:dated_parameters(conn,day) for day in current['dates']}
    require(all(value is not None and all(p is not None for p in value) for value in parameters.values()),'dated-parameters-missing')
    updates=[];proof=[]
    for record in records:
        raw=json.loads(record['plan_json'])
        output,cells=_metadata_projection(raw,book=book,version=current['book_version'],runtime=Path(request['runtime_dir']),
            record=record,days=current['dates'],parameters=parameters,operation_id=operation_id)
        if output==raw:continue
        after=json.dumps(output,ensure_ascii=False,separators=(',',':'))
        updates.append(dict(bundle_version=record['bundle_version'],as_of_date=record['as_of_date'],snapshot_id=record['snapshot_id'],
            before_sha256=digest(record['plan_json']),after_plan_json=after,after_sha256=digest(after),
            data_before=digest(data_sheet(raw)),data_after=digest(data_sheet(output))))
        proof.append(dict(as_of_date=record['as_of_date'],changed_metadata_cells=len(cells),data_unchanged=True,
            suppression_before=raw.get('metadata',{}).get(METADATA_KEY),suppression_after=output['metadata'].get(METADATA_KEY)))
    return dict(mode=CONTRACT,authority=current['authority'],updates=updates,changes=[],remaining=[],metadata_changes=proof,
        prestate_sha256=digest(dict(source=current,records=records,parameters={d:[str(p) for p in ps] for d,ps in parameters.items()})),
        before_images=[r for r in records if any(u['as_of_date']==r['as_of_date'] for u in updates)],
        source_sha256=digest(source),operation_id=operation_id)


def readback(conn,candidate,operation_id):
    from packages.application.ready_publication import check_pinned_authority
    check_pinned_authority(conn,candidate['authority'])
    bad=[]
    for update in candidate['updates']:
        row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(update['bundle_version'],update['as_of_date'])).fetchone()
        before=next(r for r in candidate['before_images'] if r['as_of_date']==update['as_of_date'])
        expected={**before,'plan_json':update['after_plan_json']}
        if row is None or dict(row)!=expected or digest(data_sheet(json.loads(row['plan_json'])))!=update['data_before']:
            bad.append(update['as_of_date'])
    return dict(operation_id=operation_id,state='applied' if not bad else 'ambiguous',verified_snapshots=len(candidate['updates'])-len(bad),
        data_unchanged=not bad,mismatches=bad,mode=CONTRACT)
