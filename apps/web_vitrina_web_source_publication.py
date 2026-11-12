"""Dated Seller Portal source/publication recovery through the one-submit launcher."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly, private_json
from apps.seller_portal_automation_guard import seller_portal_automation_lock
from apps.production_apply_contract import AmbiguousSubmit
from packages.adapters.seller_portal_web_source_collector import _dedupe, _funnel_items, _search_items, _search_group_totals_match, validate_scoped_search_evidence
from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync, _load_env_file, _closed_day_required_fetched_after
from packages.application.ready_publication import ExpectedReady, replace_ready
from packages.application.seller_funnel_snapshot_block import transform_legacy_payload as funnel_payload
from packages.application.sheet_vitrina_v1 import _column_name
from packages.application.web_source_snapshot_block import observed_search_ctr, transform_legacy_payload as search_payload
from packages.application.web_vitrina_history_group_repair import _catalog, _hash, _unit
from packages.application.web_vitrina_history_store import HistoryStore
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.business_data_heavy_admission import heavy_admitted

SOURCE_KEYS = ('seller_funnel_snapshot','web_source_snapshot')
SKU_METRICS = {'seller_funnel_snapshot':('view_count','open_card_count','ctr'), 'web_source_snapshot':('views_current','ctr_current','orders_current','position_avg')}
TOTAL_METRICS = {
    'seller_funnel_snapshot': ('total_view_count', 'total_open_card_count'),
    'web_source_snapshot': ('total_views_current', 'avg_ctr_current', 'total_orders_current', 'avg_position_avg'),
}
SEARCH_TOTAL_OPERANDS = {
    'total_orders_current': ('orders_current',),
    'avg_position_avg': ('position_avg',),
}
PG_TABLES = {
    False: [('sales_funnel_daily_raw','snapshot_date'),('search_analytics_raw','date_to')],
    True: [('web_source_sales_funnel_daily','snapshot_date'),('web_source_search_analytics_daily','date_to')],
}
PG_NUMBERS = {'view_count','open_card_count','ctr','views_current','ctr_current','orders_current','position_avg'}
_HELD_RECOVERY = ContextVar('held_web_source_recovery', default=None)


def pg_rows(rows):
    """Canonical exact images: retain all columns, normalize PG numeric/time types."""
    value=json.loads(canonical(rows))
    for row in value:
        for key,v in row.items():
            if v is not None and key in PG_NUMBERS:row[key]=format(Decimal(str(v)).normalize(),'f')
            elif v is not None and key in {'fetched_at','source_fetched_at','handoff_synced_at'}:
                row[key]=datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
    return sorted(value,key=lambda r:(r.get('snapshot_date',r.get('date_to')),r.get('date_from',''),r['nm_id']))


def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',',':'),default=str)


def digest(v):
    return 'sha256:' + hashlib.sha256(canonical(v).encode()).hexdigest()


def durable_json(path,value):
    private_json(path,value)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _held_binding(runtime, request):
    from packages.application.business_data_maintenance_pause import load_state
    from packages.application.business_data_write_barrier import barrier_status

    pause = load_state(runtime)
    barrier = barrier_status(runtime)
    window = request.get('maintenance_window_id')
    if (not window or not pause or pause['phase'] != 'held' or pause['window_id'] != window
        or barrier['active'] is not True or barrier['phase'] != 'held' or barrier['hold_confirmed'] is not True
        or barrier['window_kind'] != 'maintenance_pause' or barrier['window_id'] != window
        or barrier['plan_fingerprint'] != pause['baseline_fingerprint']):
        raise ValueError('source-recovery-maintenance-window-mismatch')
    return {'window_id': window, 'baseline_fingerprint': pause['baseline_fingerprint'],
            'barrier_fingerprint': barrier['state_fingerprint'], 'pause_sha256': digest(pause)}


def require_held_source_recovery(runtime):
    authority = _HELD_RECOVERY.get()
    if not authority or authority[:2] != (os.getpid(), Path(runtime).resolve()):
        raise ValueError('source-recovery-held-owner-required')
    if _held_binding(Path(runtime), authority[2]) != authority[3]:
        raise ValueError('source-recovery-maintenance-window-changed')
    return authority[3]


def validate_observations(request):
    observations = request['observations']
    days = sorted(set(request['dates']))
    if not days or len(days)>7 or len(days)!=len(request['dates']) or any(date.fromisoformat(d).isoformat()!=d for d in days):
        raise ValueError('source-date-scope-invalid')
    if digest(observations)!=request['source_sha256']:
        raise ValueError('source-digest-mismatch')
    pairs={(x['source_key'],x['snapshot_date']) for x in observations}
    if 'source_date_pairs' in request:
        declared=request['source_date_pairs']
        if (not isinstance(declared,list) or not declared or len(declared)>2*len(days)
            or any(not isinstance(x,dict) or set(x)!={'source_key','snapshot_date'} for x in declared)):
            raise ValueError('source-observation-scope-invalid')
        expected={(x['source_key'],x['snapshot_date']) for x in declared}
        if (len(expected)!=len(declared) or any(s not in SOURCE_KEYS or d not in days for s,d in expected)
            or {d for _,d in expected}!=set(days)):
            raise ValueError('source-observation-scope-invalid')
    else:
        expected={(s,d) for s in SOURCE_KEYS for d in days}
    if pairs!=expected or len(observations)!=len(expected):
        raise ValueError('source-observation-scope-invalid')
    for o in observations:
        day=o['snapshot_date']
        scoped=o['source_key']=='web_source_snapshot' and o.get('completeness')=='scoped_complete'
        stamp=datetime.fromisoformat(o['source_fetched_at'].replace('Z','+00:00'))
        if (o.get('contract')!='seller_portal_dated_observation_v1' or (o.get('completeness')!='complete' and not scoped)
            or o['request_period']!={'start':day,'end':day} or stamp.tzinfo is None
            or stamp < _closed_day_required_fetched_after(day)
            or stamp > datetime.now(timezone.utc) or not o['items']
            or o['supplier_identity_sha256']!=request['supplier_identity_sha256']):
            raise ValueError('source-observation-not-closed-or-unbound')
        ids=[i['nm_id'] for i in o['items']]
        if len(ids)!=len(set(ids)) or any(type(n) is not int for n in ids):
            raise ValueError('source-sku-identity-invalid')
        report=o['raw_report']
        additional=report.get('additionalErrors')
        if report.get('error') or (additional.get('errors') if isinstance(additional,dict) and set(additional)=={'errors'} else additional):
            raise ValueError('source-error-envelope')
        groups=report['data']['groups']
        if o['source_key']=='seller_funnel_snapshot':
            pages=o.get('detail_pages',[])
            if (not pages or len(pages)+1!=o['pages'] or any(p['offset']!=i*50 or p['limit']!=50 for i,p in enumerate(pages))
                or len(pages[-1]['response']['data'])>=50 or any(len(p['response']['data'])!=50 for p in pages[:-1])):
                raise ValueError('funnel-pagination-evidence-incomplete')
            reconstructed=_dedupe(_funnel_items([r for g in groups for r in g['itemsGroup']]+[r for p in pages for r in p['response']['data']]))
            if reconstructed!=o['items'] or o['pages']<2 or any(sum(g[key]['current'] for g in groups)!=sum(i[field] for i in o['items'])
                for key,field in [('viewCount','view_count'),('openCard','open_card_count')]):
                raise ValueError('funnel-source-pages-or-totals-incomplete')
        else:
            if scoped:
                scope=o.get('search_scope',{})
                items,certificate=validate_scoped_search_evidence(report=report,batches=o.get('search_batches'),
                    scope_nm_ids=scope.get('requested_nm_ids'))
                if (certificate!=scope or items!=o['items'] or o['pages']!=len(o['search_batches'])+1
                    or o.get('reported_count')!=certificate['reported_count']):
                    raise ValueError('search-scoped-source-evidence-mismatch')
                requested=publication_nm_ids(request, day)
                if not _valid_roster(requested) or not set(requested).issubset(scope['requested_nm_ids']):
                    raise ValueError('search-scoped-source-roster-unbound')
                # Source replacement must already satisfy the accepted payload
                # contract, rather than discovering incompatibility afterward.
                try:source_result(o,requested)
                except ValueError:raise ValueError('search-scoped-source-contract-invalid') from None
                continue
            if report['data']['commonInfo']['totalProducts']!=len(ids):
                raise ValueError('search-source-product-count-incomplete')
            preview_rows=[r for g in groups for r in g['items']]
            preview=_dedupe(_search_items(preview_rows))
            if len(preview_rows)!=len(preview):
                raise ValueError('search-source-pages-incomplete')
            batches=o.get('search_batches',[])
            if not batches:
                if preview!=o['items'] or o['pages']!=1:
                    raise ValueError('search-source-pages-incomplete')
                if not _search_group_totals_match(groups,preview_rows):
                    raise ValueError('search-source-group-totals-incomplete')
                continue
            if not isinstance(batches,list) or o['pages']!=len(batches)+1:
                raise ValueError('search-source-pages-incomplete')
            covered={i['nm_id']:i for i in preview};requested=set();seen=set();complete_rows=[]
            for batch in batches:
                nms=batch['requested_nm_ids'];answer=batch['response']
                if (not isinstance(nms,list) or not 1<=len(nms)<=40 or any(type(n) is not int or n<=0 for n in nms)
                    or nms!=sorted(set(nms)) or requested.intersection(nms)):
                    raise ValueError('search-source-batch-scope-invalid')
                requested.update(nms)
                errors=answer.get('additionalErrors')
                if answer.get('error') or (errors.get('errors') if isinstance(errors,dict) and set(errors)=={'errors'} else errors):
                    raise ValueError('search-source-batch-error-envelope')
                data=answer['data'];rows=[r for g in data['groups'] for r in g['items']]
                if not _search_group_totals_match(data['groups'],rows):
                    raise ValueError('search-source-batch-group-totals-incomplete')
                items=_dedupe(_search_items(rows))
                if (len(rows)!=len(items) or data['commonInfo']['totalProducts']!=len(items)
                    or any(i['nm_id'] not in nms for i in items)):
                    raise ValueError('search-source-batch-count-or-filter-invalid')
                for item in items:
                    nm=item['nm_id']
                    if nm in seen or (nm in covered and covered[nm]!=item):
                        raise ValueError('search-source-batch-overlap-or-value-drift')
                    seen.add(nm);covered[nm]=item
                complete_rows.extend(rows)
            if (not set(i['nm_id'] for i in preview).issubset(seen)
                or [covered[nm] for nm in sorted(covered)]!=o['items']):
                raise ValueError('search-source-product-count-incomplete')
            if not _search_group_totals_match(groups,complete_rows):
                raise ValueError('search-source-group-totals-incomplete')
    return observations


def source_date_pairs(request):
    """Validated exact write scope; captured neighboring rows remain CAS invariants."""
    validate_observations(request)
    return {(o['source_key'],o['snapshot_date']) for o in request['observations']}


def source_result(o, nm_ids):
    if o['source_key']=='seller_funnel_snapshot':
        return funnel_payload({'date':o['snapshot_date'],'count':len(o['items']),'items':o['items'],
            'source_fetched_at':o['source_fetched_at']}, nm_ids=nm_ids).result
    return search_payload({'date_from':o['snapshot_date'],'date_to':o['snapshot_date'],
        'count':len(o['items']),'items':o['items'],'source_fetched_at':o['source_fetched_at']}).result


def _valid_roster(ids):
    return (isinstance(ids, list) and bool(ids) and all(type(n) is int and n > 0 for n in ids)
            and ids == sorted(set(ids)))


def publication_nm_ids(request, day):
    history = request.get('history_roster')
    if 'history_roster' not in request:
        return request.get('nm_ids')
    try:
        return history['dates'][day]['nm_ids']
    except (TypeError, KeyError):
        raise ValueError('publication-history-roster-invalid') from None


def historical_publication_roster(store, edition_id, observations):
    """Read exact dated members and labels from the verified serving edition."""
    _hash(edition_id)
    if (store._current() or {}).get('current') != edition_id:
        raise ValueError('publication-history-edition-drift')
    edition = store.edition(edition_id)
    catalog_ids = store.day_catalogs(edition)
    by_date = {}
    for observation in observations:
        by_date.setdefault(observation['snapshot_date'], set()).update(SKU_METRICS[observation['source_key']])
    if not set(by_date).issubset(edition['days']):
        raise ValueError('publication-history-date-unavailable')
    result, catalogs = {}, {}
    for day, metrics in sorted(by_date.items()):
        object_id = _hash(edition['days'][day])
        catalog = _catalog(store, catalog_ids[day], None, catalogs)
        unit, _, _ = _unit(store, store.root / 'objects' / (object_id + '.sqlite3'), object_id, day, catalog, None)
        members = unit['members']
        if len(set(members)) != len(members):
            raise ValueError('publication-history-members-invalid')
        roster, labels = set(), {}
        for row_id in members:
            row = catalog['rows'][row_id]
            if row['row_kind'] != 'sku':
                continue
            scope, separator, metric = row_id.partition('|')
            raw_id = scope.removeprefix('SKU:')
            if (not separator or not raw_id.isascii() or not raw_id.isdecimal() or int(raw_id) <= 0
                or scope != f'SKU:{int(raw_id)}' or row['row_id'] != row_id):
                raise ValueError('publication-history-row-identity-invalid')
            roster.add(int(raw_id))
            if metric not in metrics:
                continue
            scope_label = row['values']['scope_label'][0]
            metric_label = row['values']['metric_label'][0]
            if any(not isinstance(label, str) or not label.strip() for label in (scope_label, metric_label)):
                raise ValueError('publication-history-row-label-invalid')
            labels[row_id] = f'{scope_label}: {metric_label}'
        if not roster or set(labels) != {f'SKU:{nm}|{metric}' for nm in roster for metric in metrics}:
            raise ValueError('publication-history-metric-membership-incomplete')
        result[day] = {'nm_ids': sorted(roster), 'row_labels': labels}
    if (store._current() or {}).get('current') != edition_id:
        raise ValueError('publication-history-edition-drift')
    return result


def publication_rosters(request, records):
    """Bind every affected historical plan to its reviewed, unchanged roster.

    The union is only the accepted source payload scope; each plan's counters
    and missing-SKU evidence continue to use that plan's own catalog.
    """
    expected = request.get('expected_nm_ids_by_as_of_date')
    if 'expected_nm_ids_by_as_of_date' in request or 'history_roster' in request:
        if (not isinstance(expected,dict) or set(expected)!={r['as_of_date'] for r in records}
            or any(not _valid_roster(ids) for ids in expected.values())):
            raise ValueError('publication-roster-scope-invalid')
        if 'history_roster' not in request:
            union=sorted({n for ids in expected.values() for n in ids})
            if not _valid_roster(request['nm_ids']) or request['nm_ids']!=union:
                raise ValueError('publication-roster-union-mismatch')
    else:
        ids=sorted(set(request['nm_ids']))
        expected={r['as_of_date']:ids for r in records}
    for record in records:
        plan=json.loads(record['plan_json'])
        actual=sorted({int(r[1].split('|')[0][4:]) for s in plan['sheets']
                       if s['sheet_name']=='DATA_VITRINA' for r in s['rows'] if r[1].startswith('SKU:')})
        if actual!=expected[record['as_of_date']]:
            raise ValueError('publication-roster-drift')
    return expected


def _resize_data_sheet(sheet, previous_count):
    if 'row_count' not in sheet and 'write_rect' not in sheet:
        return
    write_prefix = f"{sheet['write_start_cell']}:{_column_name(len(sheet['header']))}"
    if sheet.get('row_count') != previous_count or sheet.get('write_rect') != f'{write_prefix}{previous_count + 1}':
        raise ValueError('publication-history-ready-dimensions-invalid')
    sheet['row_count'] = len(sheet['rows'])
    sheet['write_rect'] = f"{write_prefix}{sheet['row_count'] + 1}"


def _new_history_rows(sheet, dates):
    """The only new rows are pinned members of the selected source/date families."""
    existing = {row[1] for row in sheet['rows']}
    added = {}
    for day in sheet['header'][2:]:
        for row_id, label in dates.get(day, {}).get('row_labels', {}).items():
            if row_id in existing:
                continue
            row = added.setdefault(row_id, {'label': label, 'dates': []})
            if row['label'] != label:
                raise ValueError('publication-history-row-label-conflict')
            row['dates'].append(day)
    return added


def non_target_digest(plan, observations, *, dated_rosters=None, added_rows=None):
    """Remove only this domain's exact cells/status/provenance before comparing."""
    value=deepcopy(plan)
    for o in observations:
        day,source=o['snapshot_date'],o['source_key']
        dated_scope = None if dated_rosters is None else {f'SKU:{nm}' for nm in dated_rosters[day]['nm_ids']}
        slots={s['slot_key'] for s in value['temporal_slots'] if s['column_date']==day}
        keys=set()
        for sheet in value['sheets']:
            if sheet['sheet_name']=='DATA_VITRINA' and day in sheet['header']:
                col=sheet['header'].index(day)
                for row in sheet['rows']:
                    scope,metric=row[1].split('|',1)
                    if ((scope=='TOTAL' and metric in TOTAL_METRICS[source])
                        or (scope.startswith('SKU:') and metric in SKU_METRICS[source]
                            and (dated_scope is None or scope in dated_scope))):
                        row[col]=None;keys.add(row[1])
            elif sheet['sheet_name']=='STATUS':
                for i,row in enumerate(sheet['rows']):
                    if any(row[0]==f'{source}[{slot}]' for slot in slots):sheet['rows'][i]=[row[0]]
        metadata=value.get('metadata',{})
        for key in keys:
            cells=metadata.get('server_cell_presentation',{})
            if key in cells:
                cells[key].pop(day,None)
                if not cells[key]:cells.pop(key)
            metadata.get('row_last_updated_at_by_row_id',{}).pop(key,None)
        for key in ('server_cell_presentation','row_last_updated_at_by_row_id'):
            if not metadata.get(key):metadata.pop(key,None)
    if added_rows:
        sheet = next(s for s in value['sheets'] if s['sheet_name'] == 'DATA_VITRINA')
        original_count = len(sheet['rows'])
        kept, removed = [], set()
        for row in sheet['rows']:
            row_id = row[1]
            if row_id not in added_rows:
                kept.append(row)
                continue
            shape = added_rows[row_id]
            expected = [shape['label'], row_id, *[None if day in shape['dates'] else '' for day in sheet['header'][2:]]]
            if row_id in removed or row != expected:
                raise ValueError('publication-history-added-row-outside-scope')
            removed.add(row_id)
        if removed != set(added_rows):
            raise ValueError('publication-history-added-row-outside-scope')
        sheet['rows'] = kept
        _resize_data_sheet(sheet, original_count)
    return digest(value)


def project(plan, observations, nm_ids, operation_id, *, history_roster=None, added_rows=None):
    result=deepcopy(plan)
    sheet=next(s for s in result['sheets'] if s['sheet_name']=='DATA_VITRINA')
    if added_rows:
        old_count = len(sheet['rows'])
        sheet['rows'].extend([shape['label'], row_id, *([''] * (len(sheet['header']) - 2))]
                             for row_id, shape in sorted(added_rows.items()))
        _resize_data_sheet(sheet, old_count)
    changes=[]
    for o in observations:
        day,source=o['snapshot_date'],o['source_key']
        if day not in sheet['header']:continue
        if history_roster is not None:
            nm_ids = history_roster['dates'][day]['nm_ids']
        dated_ids = set(nm_ids)
        col=sheet['header'].index(day)
        items={i['nm_id']:i for i in o['items'] if i['nm_id'] in dated_ids}
        missing=sorted(dated_ids-set(items))
        if not items:raise ValueError('source-has-no-applicable-sku')
        if source=='seller_funnel_snapshot':
            totals={'total_view_count':sum(i['view_count'] for i in items.values()),
                    'total_open_card_count':sum(i['open_card_count'] for i in items.values())}
        else:
            views=sum(i['views_current'] for i in items.values())
            totals = {
                'total_views_current': views,
                'avg_ctr_current': observed_search_ctr((i['ctr_current'], i['views_current']) for i in items.values()),
                'total_orders_current': sum(i['orders_current'] for i in items.values()),
                'avg_position_avg': round(sum(i['position_avg'] for i in items.values()) / len(items), 6),
            }
        for row in sheet['rows']:
            scope,metric=row[1].split('|',1)
            if scope=='TOTAL' and metric in TOTAL_METRICS[source]:
                value=totals[metric]
                scope_missing=missing
            elif scope.startswith('SKU:') and metric in SKU_METRICS[source]:
                nm=int(scope.split(':')[1])
                if nm not in dated_ids:continue
                item=items.get(nm)
                value='' if item is None or item.get(metric) is None else item[metric]/100 if metric in {'ctr','ctr_current'} else item[metric]
                scope_missing=[nm] if value=='' else []
            else:continue
            before=row[col];row[col]=value
            metadata=result.setdefault('metadata',{})
            cell={'source_key':source,'source_date':day,'source_fetched_at':o['source_fetched_at'],
                'operation_id':operation_id,'source_digest':digest(o),'source_completeness':o['completeness'],
                'completeness_state':'partial' if scope_missing else 'complete','missing_sku_count':len(scope_missing),
                'zero_fill_applied':False}
            if history_roster is not None:
                cell['history_edition_id'] = history_roster['edition_id']
            if o.get('completeness')=='scoped_complete':cell['search_scope']=deepcopy(o['search_scope'])
            if scope=='TOTAL':
                cell['metric_scope_evidence']={'operand_date':day,'applicable_scope':['SKU:'+str(n) for n in nm_ids],
                    'missing_scope':['SKU:'+str(n) for n in scope_missing],
                    'sku_metric_keys':list(SEARCH_TOTAL_OPERANDS.get(metric, SKU_METRICS[source])),'group_scopes':{}}
            metadata.setdefault('server_cell_presentation',{}).setdefault(row[1],{})[day]=cell
            metadata.setdefault('row_last_updated_at_by_row_id',{})[row[1]]=o['source_fetched_at']
            changes.append({'row_id':row[1],'date':day,'before':before,'after':value})
        for status_sheet in result['sheets']:
            if status_sheet['sheet_name']!='STATUS':continue
            slots={s['slot_key'] for s in result['temporal_slots'] if s['column_date']==day}
            for row in status_sheet['rows']:
                if any(row[0]==f'{source}[{slot}]' for slot in slots):
                    row[1]='success';row[2]=day;row[7]=len(nm_ids);row[8]=len(items);row[9]=','.join(map(str,missing))
                    row[10]=f'source_fetched_at={o["source_fetched_at"]}; source_completeness={o["completeness"]}; catalog_covered={len(items)}/{len(nm_ids)}; missing_not_zero=true; resolution_rule=accepted_closed_dated_source_recovery; operation_id={operation_id}'
                    if o.get('completeness')=='scoped_complete':row[10]+='; source_scope=bounded_nm_scope_v1; accountwide_completeness_unproven=true'
    return result,changes


class WebSourcePublicationAdapter(WebVitrinaManagementHistoryAdapter):
    def target(self, request):
        value=super().target(request)
        configured=_load_env_file(Path('/opt/wb-ai/.env')).get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','')
        if not configured or hashlib.sha256(configured.encode()).hexdigest()!=request['supplier_identity_sha256']:
            raise ValueError('source-target-supplier-mismatch')
        return value

    def _backup(self,runtime,operation_id):
        return runtime/'evidence'/(operation_id+'.before.json')

    @contextmanager
    def _write_admission(self, request, runtime, operation):
        if 'maintenance_window_id' not in request:
            with heavy_admitted(runtime, operation=operation):
                yield
            return
        from apps.business_data_maintenance import _ExclusiveRestoreLock
        from packages.application.business_data_procedure_admission import AdmissionLease, admission_idle
        from packages.application.business_data_write_barrier import _BarrierLock

        with _ExclusiveRestoreLock(runtime), _BarrierLock(runtime):
            binding = _held_binding(runtime, request)
            activity = admission_idle(runtime)
            if not activity['ready'] or not activity['idle']:
                raise ValueError('source-recovery-admission-not-idle')
            token = _HELD_RECOVERY.set((os.getpid(), runtime.resolve(), request, binding))
            try:
                lease = AdmissionLease.held_web_source_recovery(runtime)
                try:
                    if lease.fd is None:
                        raise ValueError('source-recovery-admission-missing')
                    with lease.entered(), heavy_admitted(runtime, operation=operation):
                        yield
                finally:
                    lease.close()
            finally:
                _HELD_RECOVERY.reset(token)
                if _held_binding(runtime, request) != binding:
                    raise ValueError('source-recovery-maintenance-window-changed')

    def _history_store(self):
        contract_path = ROOT / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json'
        contract = json.loads(contract_path.read_text())
        root = Path(contract['candidate_root'])
        if not root.is_absolute():
            raise ValueError('publication-history-target-invalid')
        return HistoryStore(root / 'history')

    @contextmanager
    def _lock_history_roster(self, request):
        if 'history_roster' not in request:
            yield
            return
        # The serving edition cannot change between candidate verification and
        # native commit. Use the same lock as HistoryStore's edition writers.
        with (self._history_store().root / 'writer.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('publication-history-writer-busy') from None
            yield

    def _pg_connect(self, target=False):
        import psycopg2
        env=_load_env_file(Path('/opt/wb-ai/.env'))
        prefix='' if target else 'WEB_SOURCE_SRC_'
        return psycopg2.connect(**{name:env[prefix+key] for name,key in
            [('host','PGHOST'),('port','PGPORT'),('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD')]})

    @contextmanager
    def _pg_connections(self, *, locked=False):
        connections={}
        try:
            for target,tables in PG_TABLES.items():
                conn=self._pg_connect(target);connections[target]=conn
                if locked:
                    with conn.cursor() as cur:
                        cur.execute('SET LOCAL lock_timeout = \'5s\'')
                        for table,_ in tables:cur.execute(f'LOCK TABLE public.{table} IN SHARE ROW EXCLUSIVE MODE')
                else:conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            yield connections
        finally:
            for conn in connections.values():
                conn.rollback();conn.close()

    def _pg_read(self,conn,tables,request):
        out={}
        with conn.cursor() as cur:
            for table,col in tables:
                # The handoff API reads every date_from for date_to. Bind these
                # neighboring rows too, while writes replace only exact-day rows.
                cur.execute(f'SELECT * FROM public.{table} WHERE {col}=ANY(%s::date[]) ORDER BY {col},nm_id',(request['dates'],))
                out[table]=pg_rows([dict(zip([c.name for c in cur.description],row)) for row in cur.fetchall()])
        return out

    def _pg_capture(self,request,connections=None):
        if connections is None:
            with self._pg_connections() as opened:return self._pg_capture(request,opened)
        rows={};identities={}
        for target,conn in connections.items():
            rows.update(self._pg_read(conn,PG_TABLES[target],request))
            with conn.cursor() as cur:
                cur.execute('SELECT current_database(),current_user,inet_server_addr()::text,inet_server_port()')
                identities[str(target)]=list(cur.fetchone())
        return rows,identities

    def _source_rows(self,request,target,stamp,before):
        after=deepcopy(before)
        for o in request['observations']:
            funnel=o['source_key']=='seller_funnel_snapshot';day=o['snapshot_date']
            table=PG_TABLES[target][0 if funnel else 1][0]
            # Explicit multi-day reports are not this recovery's write target.
            keep=[r for r in after[table] if (r.get('snapshot_date',r.get('date_to'))!=day or
                (not funnel and r['date_from']!=day))]
            for item in o['items']:
                row=deepcopy(item)
                if funnel:
                    row.update(snapshot_date=day)
                    row['source_fetched_at' if target else 'fetched_at']=o['source_fetched_at']
                else:
                    row.update(date_from=day,date_to=day,raw_json=o['raw_report'])
                    if not target:row['fetched_at']=o['source_fetched_at']
                if target:row['handoff_synced_at']=stamp
                keep.append(row)
            after[table]=pg_rows(keep)
        return after

    def _pg_replace(self,conn,request,tables,images):
        from psycopg2.extras import Json, execute_batch
        pairs=source_date_pairs(request)
        with conn.cursor() as cur:
            for table,col in tables:
                source=SOURCE_KEYS[0 if col=='snapshot_date' else 1]
                days=sorted(d for s,d in pairs if s==source)
                if not days:continue
                exact=' AND date_from=date_to' if col=='date_to' else ''
                cur.execute(f'DELETE FROM public.{table} WHERE {col}=ANY(%s::date[]){exact}',(days,))
                rows=[r for r in images[table] if r[col] in days and (col!='date_to' or r['date_from']==r['date_to'])]
                if rows:
                    columns=list(rows[0])
                    execute_batch(cur,f'INSERT INTO public.{table} ('+','.join(columns)+') VALUES ('+','.join('%s' for _ in columns)+')',
                        [tuple(Json(r[k]) if k=='raw_json' else r[k] for k in columns) for r in rows])

    def _part_path(self,runtime,operation_id,target):
        return runtime/'evidence'/(operation_id+('.handoff' if target else '.raw')+'.write-ahead.json')

    def _source_apply(self,request,operation_id,candidate,runtime,connections):
        for target,conn in connections.items():
            tables=PG_TABLES[target]
            before={t:candidate['before_images'][t] for t,_ in tables}
            if digest(self._pg_read(conn,tables,request))!=digest(before):raise ValueError('source-part-before-drift')
            with conn.cursor() as cur:
                cur.execute('SELECT transaction_timestamp()');stamp=cur.fetchone()[0].isoformat()
            after=self._source_rows(request,target,stamp,before)
            self._pg_replace(conn,request,tables,after)
            if digest(self._pg_read(conn,tables,request))!=digest(after):raise ValueError('source-staged-after-mismatch')
            # Durable exact after image precedes commit, closing the crash window.
            part={'operation_id':operation_id,'request_sha256':digest(request),'candidate_sha256':digest(candidate),
                  'target':target,'stamp':stamp,'before':before,'after':after}
            path=self._part_path(runtime,operation_id,target);durable_json(path,part)
            if digest(json.loads(path.read_text()))!=digest(part):raise ValueError('source-journal-verification-failed')
            conn.commit()

    def _source_states(self,request,operation_id,candidate,runtime,connections=None):
        actual,identities=self._pg_capture(request,connections)
        if identities!=candidate['pg_targets']:return {'target':'drift'}
        states={}
        for target,tables in PG_TABLES.items():
            name='handoff' if target else 'raw'
            before={t:candidate['before_images'][t] for t,_ in tables}
            current={t:actual[t] for t,_ in tables}
            path=self._part_path(runtime,operation_id,target)
            after=None
            if path.exists():
                part=json.loads(path.read_text())
                if (part['operation_id']!=operation_id or part['request_sha256']!=digest(request)
                    or part['candidate_sha256']!=digest(candidate) or part['target']!=target or part['before']!=before):
                    raise ValueError('source-journal-identity-mismatch')
                after=part['after']
                if digest(after)!=digest(self._source_rows(request,target,part['stamp'],before)):
                    raise ValueError('source-journal-after-mismatch')
            states[name]='after' if after is not None and digest(current)==digest(after) else 'before' if digest(current)==digest(before) else 'drift'
        return states

    def _materialized(self,request):
        from urllib.request import urlopen
        from urllib.parse import urlencode
        for o in request['observations']:
            day=o['snapshot_date'];source=o['source_key']
            path='/v1/sales-funnel/daily?'+urlencode({'date':day}) if source=='seller_funnel_snapshot' else '/v1/search-analytics/snapshot?'+urlencode({'date_from':day,'date_to':day})
            with urlopen('http://127.0.0.1:8000'+path,timeout=10) as r:payload=json.load(r)
            observed={int(i['nm_id']):i for i in payload['items']}
            wanted={int(i['nm_id']):i for i in o['items']}
            if set(observed)!=set(wanted):raise ValueError('materialized-source-sku-mismatch')
            for nm,item in wanted.items():
                if any(observed[nm].get(k)!=v for k,v in item.items()):raise ValueError('materialized-source-value-mismatch')
            state=ShellBackedWebSourceCurrentSync()._load_closed_day_source_state(source,day)
            if state is None or state.row_count!=len(wanted) or datetime.fromisoformat(state.fetched_at.replace('Z','+00:00'))!=datetime.fromisoformat(o['source_fetched_at'].replace('Z','+00:00')):
                raise ValueError('materialized-source-clock-mismatch')

    def _publication_images(self, request, conn):
        current=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
        config=[dict(r) for r in conn.execute('SELECT * FROM registry_upload_config_v2 WHERE bundle_version=? ORDER BY nm_id',(current,))]
        dates = ','.join('?' for _ in request['dates'])
        records = [dict(r) for r in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND EXISTS ("
            "SELECT 1 FROM json_each(plan_json,'$.date_columns') d WHERE d.value IN ("+dates+")) ORDER BY as_of_date",
            (current, *request['dates']))]
        if not records or len(records)>16:raise ValueError('publication-ready-scope-invalid')
        slots=[dict(r) for r in conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key IN (?,?) AND snapshot_date IN ("+dates+") ORDER BY source_key,snapshot_date,snapshot_role",(*SOURCE_KEYS,*request['dates']))]
        closure=[dict(r) for r in conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key IN (?,?) AND target_date IN ("+dates+") ORDER BY source_key,target_date,slot_kind",(*SOURCE_KEYS,*request['dates']))]
        return {'ready':records,'slots':slots,'closure':closure,'config':config}

    def _validate_history_roster(self, request, observations, records):
        if 'history_roster' not in request:
            return None
        history = request['history_roster']
        if not isinstance(history, dict) or set(history) != {'edition_id', 'dates'}:
            raise ValueError('publication-history-roster-invalid')
        authority = historical_publication_roster(self._history_store(), history['edition_id'], observations)
        if canonical(authority) != canonical(history['dates']):
            raise ValueError('publication-history-roster-drift')
        union = sorted({nm for dated in authority.values() for nm in dated['nm_ids']})
        if not _valid_roster(request.get('nm_ids')) or request['nm_ids'] != union:
            raise ValueError('publication-history-roster-union-mismatch')
        ready_dates = {day for record in records for day in json.loads(record['plan_json'])['date_columns']}
        if not set(authority).issubset(ready_dates):
            raise ValueError('publication-history-ready-date-unavailable')
        return history

    def build(self,request,operation_id,conn,pg_connections=None):
        observations=validate_observations(request)
        maintenance = {'maintenance_binding': _held_binding(Path(request['runtime_dir']).resolve(), request)} if 'maintenance_window_id' in request else {}
        if request['phase']=='source':
            if 'history_roster' in request:
                raise ValueError('publication-history-roster-requires-publication')
            before,identities=self._pg_capture(request,pg_connections)
            if any(r['date_from']!=r['date_to'] for table in ('search_analytics_raw','web_source_search_analytics_daily') for r in before[table]):
                raise ValueError('source-handoff-period-overlap')
            for o in observations:
                if o.get('completeness')!='scoped_complete':continue
                scope=set(o['search_scope']['requested_nm_ids'])
                if any(r['nm_id'] not in scope for table in ('search_analytics_raw','web_source_search_analytics_daily')
                       for r in before[table] if r['date_to']==o['snapshot_date']):
                    raise ValueError('source-search-scope-excludes-existing-sku')
            return {'operation_id':operation_id,'phase':'source','prestate_sha256':digest(before),
                'before_images':before,'pg_targets':identities,'source_sha256':request['source_sha256'], **maintenance}
        if request['phase']!='publication':raise ValueError('phase-invalid')
        before = self._publication_images(request, conn)
        records = before['ready']
        rosters=publication_rosters(request,records)
        history = self._validate_history_roster(request, observations, records)
        dated_rosters = history['dates'] if history else None
        additions = {}
        updates=[];changes=[]
        for record in records:
            old=json.loads(record['plan_json'])
            sheet = next(s for s in old['sheets'] if s['sheet_name'] == 'DATA_VITRINA')
            added = _new_history_rows(sheet, dated_rosters) if history else None
            if history:
                needed_totals = {f'TOTAL|{metric}' for o in observations if o['snapshot_date'] in sheet['header']
                                 for metric in TOTAL_METRICS[o['source_key']]}
                if not needed_totals.issubset(row[1] for row in sheet['rows']):
                    raise ValueError('publication-history-ready-total-missing')
            new,delta=project(old,observations,rosters[record['as_of_date']],operation_id,
                              history_roster=history,added_rows=added)
            if non_target_digest(old,observations,dated_rosters=dated_rosters)!=non_target_digest(
                new,observations,dated_rosters=dated_rosters,added_rows=added,
            ):
                raise ValueError('non-target-publication-content-changed')
            updates.append({'bundle_version':record['bundle_version'],'as_of_date':record['as_of_date'],'after_plan_json':canonical(new)})
            changes.extend({**c,'outer_date':record['as_of_date']} for c in delta)
            if added:
                additions[record['as_of_date']] = added
        after=deepcopy(before)
        for row,update in zip(after['ready'],updates):row['plan_json']=update['after_plan_json']
        slots={(r['source_key'],r['snapshot_date'],r['snapshot_role']):r for r in after['slots']}
        closure={(r['source_key'],r['target_date'],r['slot_kind']):r for r in after['closure']}
        for o in observations:
            source,day=o['source_key'],o['snapshot_date'];stamp=o['source_fetched_at']
            key=(source,day,'accepted_closed_day_snapshot')
            slots[key]={'source_key':source,'snapshot_date':day,'snapshot_role':key[2],
                'captured_at':stamp,'payload_json':canonical(asdict(source_result(o,publication_nm_ids(request, day))))}
            key=(source,day,'yesterday_closed')
            closure[key]={**closure.get(key,{'source_key':source,'target_date':day,'slot_kind':key[2],'attempt_count':1}),
                'state':'success','next_retry_at':None,'last_reason':'dated_source_recovery:'+operation_id,
                'last_attempt_at':stamp,'last_success_at':stamp,'accepted_at':stamp}
        after['slots']=[slots[k] for k in sorted(slots)]
        after['closure']=[closure[k] for k in sorted(closure)]
        return {'operation_id':operation_id,'phase':'publication','prestate_sha256':digest(before),
            'before_images':before,'after_images':after,'updates':updates,'changes':changes,'source_sha256':request['source_sha256'],
            **({'added_rows': additions} if history else {}), **maintenance}

    def preview(self,request,operation_id):
        runtime,db=self.target(request);backup=self._backup(runtime,operation_id)
        if backup.exists():
            retained=json.loads(backup.read_text())
            if digest(retained['request'])!=digest(request):raise ValueError('operation-request-mismatch')
            candidate=retained['candidate']
        else:
            with readonly(db) as conn:
                conn.execute('BEGIN');candidate=self.build(request,operation_id,conn)
        return {'operation_id':operation_id,'target':str(db),'scope':{'phase':request['phase'],'dates':request['dates'],
                'source_date_pairs':[{'source_key':s,'snapshot_date':d} for s,d in sorted(source_date_pairs(request))],
                **({'maintenance_window_id':request['maintenance_window_id']} if 'maintenance_window_id' in request else {}),
                **({'expected_nm_ids_by_as_of_date':request['expected_nm_ids_by_as_of_date']}
                   if request['phase']=='publication' and 'expected_nm_ids_by_as_of_date' in request else {}),
                **({'history_edition_id':request['history_roster']['edition_id'],
                    'nm_ids_by_date':{day: scope['nm_ids'] for day, scope in request['history_roster']['dates'].items()},
                    'added_rows':sum(len(rows) for rows in candidate['added_rows'].values())}
                   if 'history_roster' in request else {}),
                **({'search_scopes':[{'snapshot_date':o['snapshot_date'],**o['search_scope']}
                                    for o in request['observations'] if o.get('completeness')=='scoped_complete']}
                   if any(o.get('completeness')=='scoped_complete' for o in request['observations']) else {}),
                'changed_cells':len(candidate.get('changes',[]))},
            'prestate_sha256':candidate['prestate_sha256'],'candidate_sha256':digest(candidate),'candidate':candidate,
            'recovery':{'kind':'exact-source-and-ready-before-images','path':str(backup)}}

    def apply(self,request,operation_id,preview):
        runtime,_=self.target(request)
        try:
            return self._apply(request,operation_id,preview)
        except Exception:
            if self._backup(runtime,operation_id).exists():
                raise AmbiguousSubmit('source-publication-operation-requires-readback') from None
            raise

    def _apply(self,request,operation_id,preview):
        runtime,db=self.target(request)
        with self._write_admission(request,runtime,'web_source_publication'), warehouse_functional_job_lock(runtime,blocking=False),warehouse_sync_lock(runtime,blocking=False), seller_portal_automation_lock(runtime_dir=runtime,owner='web_source_publication',purpose=request['phase'],run_id=operation_id,expected_max_seconds=600):
            with ExitStack() as stack:
                stack.enter_context(self._lock_history_roster(request))
                pg=stack.enter_context(self._pg_connections(locked=True)) if request['phase']=='source' else None
                conn=stack.enter_context(sqlite3.connect(db,timeout=30))
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                self.target(request);candidate=self.build(request,operation_id,conn,pg_connections=pg)
                if digest(candidate)!=preview['candidate_sha256']:raise ValueError('candidate-cas-drift')
                backup=self._backup(runtime,operation_id)
                backup.parent.mkdir(exist_ok=True)
                if backup.exists():raise ValueError('operation-already-submitted-readback-only')
                if request['phase']=='publication':self._materialized(request)
                durable_json(backup,{'operation_id':operation_id,'candidate':candidate,'request':request})
                if digest(json.loads(backup.read_text())['candidate'])!=preview['candidate_sha256']:raise ValueError('backup-verification-failed')
                if request['phase']=='source':
                    self._source_apply(request,operation_id,candidate,runtime,pg)
                else:
                    for update,before in zip(candidate['updates'],candidate['before_images']['ready']):
                        replace_ready(conn,expected=ExpectedReady(update['bundle_version'],update['as_of_date'],before['plan_json']),plan_json=update['after_plan_json'])
                    for o in request['observations']:
                        source,day=o['source_key'],o['snapshot_date'];stamp=o['source_fetched_at']
                        payload=canonical(asdict(source_result(o,publication_nm_ids(request, day))))
                        conn.execute('INSERT INTO temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(source_key,snapshot_date,snapshot_role) DO UPDATE SET captured_at=excluded.captured_at,payload_json=excluded.payload_json',
                            (source,day,'accepted_closed_day_snapshot',stamp,payload))
                        conn.execute("INSERT INTO temporal_source_closure_state(source_key,target_date,slot_kind,state,attempt_count,next_retry_at,last_reason,last_attempt_at,last_success_at,accepted_at) VALUES(?,?,?,'success',1,NULL,?,?,?,?) ON CONFLICT(source_key,target_date,slot_kind) DO UPDATE SET state='success',next_retry_at=NULL,last_reason=excluded.last_reason,last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,accepted_at=excluded.accepted_at",
                            (source,day,'yesterday_closed','dated_source_recovery:'+operation_id,stamp,stamp,stamp))
                    if self._publication_state(request,operation_id,candidate,conn)!='after':raise ValueError('publication-staged-state-mismatch')
                conn.commit()
        return {'operation_id':operation_id,'disposition':'submitted'}

    def _retained(self,request,operation_id,runtime):
        retained=json.loads(self._backup(runtime,operation_id).read_text())
        if retained['operation_id']!=operation_id or digest(retained['request'])!=digest(request):
            raise ValueError('operation-request-mismatch')
        return retained['candidate']

    def _publication_state(self,request,operation_id,candidate,conn):
        actual=self._publication_images(request,conn)
        return 'after' if digest(actual)==digest(candidate['after_images']) else 'before' if digest(actual)==digest(candidate['before_images']) else 'drift'

    def _publication_target_state(self, request, candidate, conn):
        pairs = source_date_pairs(request)
        targets = {}
        for change in candidate['changes']:
            targets.setdefault(change['outer_date'], {}).setdefault(change['date'], set()).add(change['row_id'])

        def image(images):
            ready = []
            for record in images['ready']:
                dated = targets.get(record['as_of_date'])
                if not dated:
                    continue
                plan = json.loads(record['plan_json'])
                sheet = next(s for s in plan['sheets'] if s['sheet_name'] == 'DATA_VITRINA')
                cells = plan.get('metadata', {}).get('server_cell_presentation', {})
                values = {}
                for day, row_ids in sorted(dated.items()):
                    if day in sheet['header']:
                        col = sheet['header'].index(day)
                        values[day] = [[row[0],row[1],row[col],cells.get(row[1], {}).get(day)]
                                       for row in sheet['rows'] if row[1] in row_ids and len(row) > col]
                slots = [slot for slot in plan['temporal_slots'] if slot['column_date'] in dated]
                status_keys = {f"{source}[{slot['slot_key']}]" for slot in slots
                               for source in SOURCE_KEYS if (source, slot['column_date']) in pairs}
                statuses = [row for s in plan['sheets'] if s['sheet_name'] == 'STATUS'
                            for row in s['rows'] if row[0] in status_keys]
                ready.append([record['bundle_version'],record['as_of_date'],values,slots,statuses])
            return {
                'ready': ready,
                'slots': [row for row in images['slots'] if row['snapshot_role'] == 'accepted_closed_day_snapshot'
                          and (row['source_key'],row['snapshot_date']) in pairs],
                'closure': [row for row in images['closure'] if row['slot_kind'] == 'yesterday_closed'
                            and (row['source_key'],row['target_date']) in pairs],
            }

        actual = image(self._publication_images(request, conn))
        return 'after' if digest(actual) == digest(image(candidate['after_images'])) else 'drift'

    def readback(self,request,operation_id,*,target_only=False):
        runtime,db=self.target(request);backup=self._backup(runtime,operation_id)
        if not backup.exists():return {'operation_id':operation_id,'state':'not_submitted'}
        candidate=self._retained(request,operation_id,runtime)
        if request['phase']=='source':states=self._source_states(request,operation_id,candidate,runtime)
        else:
            with readonly(db) as conn:
                conn.execute('BEGIN')
                state = (self._publication_target_state(request,candidate,conn) if target_only
                         else self._publication_state(request,operation_id,candidate,conn))
                states={'publication':state}
        bad=[name+':'+state for name,state in states.items() if state!='after']
        if not bad:
            try:self._materialized(request)
            except Exception as exc:bad.append('materialized:'+type(exc).__name__)
        return {'operation_id':operation_id,'state':'applied' if not bad else 'ambiguous',
            'parts':states,'mismatches':bad,'recovery_reference':str(backup)}

    def rollback(self,request,operation_id):
        """Restore only an exact before/our write-ahead after mixture under CAS."""
        runtime,db=self.target(request)
        candidate=self._retained(request,operation_id,runtime)
        with self._write_admission(request,runtime,'web_source_publication_rollback'), warehouse_functional_job_lock(runtime,blocking=False),warehouse_sync_lock(runtime,blocking=False), seller_portal_automation_lock(runtime_dir=runtime,owner='web_source_publication',purpose='rollback',run_id=operation_id+'-rollback',expected_max_seconds=600):
            self.target(request)
            if 'maintenance_window_id' in request and require_held_source_recovery(runtime) != candidate['maintenance_binding']:
                raise ValueError('source-recovery-maintenance-window-changed')
            if request['phase']=='source':
                with self._pg_connections(locked=True) as connections:
                    states=self._source_states(request,operation_id,candidate,runtime,connections)
                    if any(s not in {'before','after'} for s in states.values()):raise ValueError('rollback-after-state-drift')
                    # Both stores are locked and all parts checked before any write.
                    # A partial recovery remains an exact before/after mixture.
                    for target,conn in connections.items():
                        if states['handoff' if target else 'raw']=='before':continue
                        tables=PG_TABLES[target];before={t:candidate['before_images'][t] for t,_ in tables}
                        self._pg_replace(conn,request,tables,before)
                        if digest(self._pg_read(conn,tables,request))!=digest(before):raise ValueError('rollback-staged-state-mismatch')
                        conn.commit()
                if any(s!='before' for s in self._source_states(request,operation_id,candidate,runtime).values()):
                    raise ValueError('rollback-readback-drift')
            else:
                with sqlite3.connect(db,timeout=30) as conn:
                    conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                    state=self._publication_state(request,operation_id,candidate,conn)
                    if state=='drift':raise ValueError('rollback-after-state-drift')
                    if state=='after':
                        for after,before in zip(candidate['updates'],candidate['before_images']['ready']):
                            replace_ready(conn,expected=ExpectedReady(after['bundle_version'],after['as_of_date'],after['after_plan_json']),plan_json=before['plan_json'])
                        pairs=source_date_pairs(request)
                        for source,day in pairs:
                            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'",(source,day))
                            conn.execute("DELETE FROM temporal_source_closure_state WHERE source_key=? AND target_date=? AND slot_kind='yesterday_closed'",(source,day))
                        for table,rows in [('temporal_source_slot_snapshots',candidate['before_images']['slots']),('temporal_source_closure_state',candidate['before_images']['closure'])]:
                            for row in rows:
                                if table=='temporal_source_slot_snapshots' and row['snapshot_role']!='accepted_closed_day_snapshot':continue
                                if table=='temporal_source_closure_state' and row['slot_kind']!='yesterday_closed':continue
                                key=(row['source_key'],row.get('snapshot_date',row.get('target_date')))
                                if key not in pairs:continue
                                columns=list(row)
                                conn.execute(f'INSERT INTO {table} ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row[k] for k in columns))
                    if self._publication_state(request,operation_id,candidate,conn)!='before':raise ValueError('rollback-staged-state-mismatch')
                    conn.commit()
        return {'operation_id':operation_id,'state':'restored'}
