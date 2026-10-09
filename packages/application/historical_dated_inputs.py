"""Read-only native dated inputs shared by typed History owners.

Extracted from pinned operator_policy_history 671383466c81246bd19dd73384448bde48cc1f00.
The original payloads, byte bound and refusal codes are preserved. This module
admits no source, publication or receipt authority.
"""
import json

MAX_BYTES = 160*1024**2

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def require(condition,code):
    if not condition:raise ValueError('policy_history_'+code)

def selected(conn,dates,today):
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    from packages.application.web_vitrina_window_v3 import _ReadyHeader,_select_bindings
    rows=[dict(r) for r in conn.execute('SELECT ready.* FROM sheet_vitrina_v1_ready_snapshots ready JOIN registry_upload_current_state current ON current.slot=1 AND current.bundle_version=ready.bundle_version ORDER BY ready.activated_at DESC,ready.refreshed_at DESC,ready.as_of_date DESC')]
    require(sum(len(row['plan_json'].encode()) for row in rows)<=MAX_BYTES,'source_size_limit')
    headers=[]
    for row in rows:
        plan=_deserialize_sheet_vitrina_plan(row['plan_json'])
        headers.append(_ReadyHeader(row['bundle_version'],row['as_of_date'],plan.snapshot_id,row['activated_at'],row['refreshed_at'],tuple(plan.date_columns),canonical(plan.metadata.get('fbs_accounting_bindings',{})),0))
    active=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
    require(active,'active_bundle_missing')
    bindings,_=_select_bindings(conn,headers,dates,active[0],today)
    source={binding.date:binding.source_key for binding in bindings}
    require(set(source)==set(dates) and all(source.values()),'dated_ready_inputs_missing')
    return [(row,sorted(day for day,key in source.items() if key==(row['bundle_version'],row['as_of_date']))) for row in rows if (row['bundle_version'],row['as_of_date']) in set(source.values())]

def dated_slice(encoded,day):
    payload=json.loads(encoded);sheet=next(s for s in payload['sheets'] if s['sheet_name']=='DATA_VITRINA');require(day in sheet['header'],'dated_column_missing');index=sheet['header'].index(day)
    meta=payload.get('metadata',{})
    return {'date':day,'rows':[[r[0],r[1],r[index] if len(r)>index else None] for r in sheet['rows']],
        'presentation':{k:v[day] for k,v in meta.get('server_cell_presentation',{}).items() if day in v},
        'native_metadata':{k:({a:b for a,b in v[day].items() if a not in ('book_version','ready_target')} if k=='fbs_accounting_bindings' else v[day]) for k,v in meta.items() if isinstance(v,dict) and day in v and k not in ('server_cell_presentation','fbs_accounting_targets')}}

def book_lineage(runtime,encoded,day):
    """A new book envelope may retain the exact immutable dated cost image.

    Removing volatile book/ready target IDs from the cell digest is allowed
    only alongside this independently loaded native immutable book proof.
    """
    from packages.application import fbs_accounting_runtime as accounting
    meta=json.loads(encoded).get('metadata',{});bound=meta.get('fbs_accounting_bindings',{}).get(day)
    if bound is None:return None
    require(isinstance(bound,dict) and bound.get('book_version'),'dated_book_binding_missing')
    target=meta.get('fbs_accounting_targets',{}).get(day,meta.get('ready_publication_target'))
    require(target and target==bound.get('ready_target'),'dated_book_target_changed')
    book,version=accounting.load(runtime.runtime_dir,version=bound['book_version'])
    payload=book['presentations'].get(day)
    require(payload and bound.get('date')==day and bound.get('source')==accounting.SOURCE and bound.get('presentation_version')==payload.get('version_id') and bound.get('quality')==payload.get('quality') and bound.get('effective_date')==book['effective_date'],'dated_book_binding_changed')
    fields=('wb_days','retained_days','shared_days','presentations')
    require(all(day in book[field] for field in fields),'dated_book_inputs_missing')
    return {field:book[field][day] for field in fields}
