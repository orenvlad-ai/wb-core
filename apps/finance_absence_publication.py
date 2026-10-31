"""Missing-only diagnostic publication from complete sparse weekly finance evidence."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly
from apps.production_apply_contract import AmbiguousSubmit
from packages.application.web_vitrina_management_history import digest, data_sheet, non_target_digest
from packages.application.canonical_wb_cost_resolver import proven_finance_no_operation_skus

ALLOWED_DATES = {'2026-10-' + str(d) for d in range(18,26)}
METRIC = 'sales_without_cost_rub'

def capture_source(conn, *, dates, bundle_version, nm_ids):
    if not dates or dates != sorted(set(dates)) or not set(dates) <= ALLOWED_DATES:
        raise ValueError('finance-absence-date-scope-invalid')
    if not nm_ids or nm_ids != sorted(set(nm_ids)):
        raise ValueError('finance-absence-roster-invalid')
    observations = {}; weeks = {}
    for day in dates:
        syncs = [dict(r) for r in conn.execute('SELECT * FROM wb_finance_weekly_sync WHERE week_start<=? AND week_end>=?',(day,day))]
        if len(syncs) != 1:
            raise ValueError('finance-absence-unique-week-required')
        sync = syncs[0]; identity = (sync['seller_id'],sync['week_start'],sync['week_end'])
        records = {}
        for table in ('wb_finance_weekly_reports','wb_finance_weekly_sku_aggregates'):
            rows = [dict(r) for r in conn.execute('SELECT * FROM '+table+' WHERE seller_id=? AND week_start=? AND week_end=?',identity)]
            records[table] = sorted(rows,key=lambda r:json.dumps(r,sort_keys=True))
        proof = {'sync':sync,**records}; proof_id = digest(proof); weeks[proof_id] = proof
        absent = proven_finance_no_operation_skus(conn,as_of_date=date.fromisoformat(day))
        # This repair exists specifically for proven sparse days. An invalid
        # proof and a complete non-sparse day must not be conflated here.
        if not absent:
            raise ValueError('finance-absence-proof-unavailable')
        skurows = {str(r['nm_id']):r for r in records['wb_finance_weekly_sku_aggregates'] if str(r['nm_id'])!='__account__'}
        if len(skurows) != len(records['wb_finance_weekly_sku_aggregates'])-1:
            raise ValueError('finance-absence-duplicate-sku')
        amounts = {}; kinds = {}
        for nm in nm_ids:
            row = skurows.get(str(nm))
            if row is None: raise ValueError('finance-absence-sku-unproven')
            daily = [r for r in json.loads(row['coverage_json'])['daily_rows'] if r['operation_date']==day]
            if len(daily)==1:
                amount = Decimal(str(daily[0]['uncovered_sales_revenue_rub'])); kind='exact_daily_finance'
            elif not daily and nm in absent:
                amount = Decimal(0); kind='complete_week_no_operation'
            else: raise ValueError('finance-absence-daily-proof-missing')
            if not amount.is_finite() or amount < 0 or amount != Decimal(format(amount,'.4f')): raise ValueError('finance-absence-invalid-amount')
            amounts[str(nm)] = format(amount,'.4f'); kinds[str(nm)] = kind
        observations[day] = {'week_proof':proof_id,'skus':amounts,'kinds':kinds,
            'total':format(sum(map(Decimal,amounts.values()),Decimal(0)),'.4f')}
    source = {'dates':dates,'bundle_version':bundle_version,'nm_ids':nm_ids,'weeks':weeks,'observations':observations}
    return {**source,'snapshot_id':'finance-absence:'+digest(source)}

STALE_MODE = 'stale_diagnostic_projection'
STALE_DAY = '2026-10-18'
STALE_RECOVERY = 'recovery_b36e898c13e9568f755b22b7cd42c236'
STALE_RECEIPT = 'finance-cost-estimates-1501-20261027.finance-stale-intent.result.json'

def capture_stale_source(conn, *, runtime, source):
    """Prove the exact legacy diagnostic against the retained PR1501 before-image."""
    def require(ok, reason):
        if not ok: raise ValueError('finance-stale-'+reason)
    require(STALE_DAY in source['dates'], 'date-required')
    receipt=json.loads((Path(runtime)/'evidence'/STALE_RECEIPT).read_text())
    op_row=conn.execute('SELECT * FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?',(STALE_RECOVERY,)).fetchone()
    require(op_row is not None,'operation-missing');op=dict(op_row)
    require(op['lifecycle_state']=='retained' and op['operation_kind']=='canonical_cost_bounded_publication'
        and op['after_digest']==op['plan_fingerprint'] and not op['last_error'],'operation-not-retained')
    require(receipt.get('recovery')==STALE_RECOVERY and receipt['result'].get('status')=='applied'
        and receipt['result'].get('fingerprint')==op['plan_fingerprint']
        and receipt['result'].get('non_target_preserved') is True,'receipt-linkage')
    artifacts=conn.execute("SELECT * FROM sheet_vitrina_v1_recovery_artifacts WHERE operation_id=? AND artifact_kind='undo'",(STALE_RECOVERY,)).fetchall()
    require(len(artifacts)==1,'artifact-missing');artifact=dict(artifacts[0])
    undo=[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_recovery_undo_rows WHERE operation_id=? ORDER BY sequence_no',(STALE_RECOVERY,))]
    require(undo and all(r['status']=='verified' for r in undo),'undo-not-verified')
    images=[{'sequence_no':r['sequence_no'],'table':r['table_name'],'key':json.loads(r['key_json']),
        'before':json.loads(r['before_json']) if r['before_json'] is not None else None,
        'after':json.loads(r['after_json']) if r['after_json'] is not None else None,'action':r['action']} for r in undo]
    require(artifact['state']=='verified' and digest(images)==artifact['digest']==op['checkpoint_digest'],'undo-digest')
    records=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
        (source['bundle_version'],STALE_DAY)).fetchall()
    require(len(records)==1,'ready-identity');record=dict(records[0]);plan=json.loads(record['plan_json'])
    require(record['snapshot_id']=='2026-10-18__2026-10-22__sheet_vitrina_v1_temporal_live_v1__current'
        and plan['snapshot_id']==record['snapshot_id'],'ready-version')
    sheet=data_sheet(plan);index=sheet['header'].index(STALE_DAY)
    amounts={r[1]:r[index] for r in sheet['rows'] if r[1].startswith('SKU:') and r[1].endswith('|'+METRIC)}
    current=source['weeks'][source['observations'][STALE_DAY]['week_proof']]
    sync=current['sync'];currents={str(r['nm_id']):r for r in current['wb_finance_weekly_sku_aggregates']}
    cells={}
    for image in images:
        old=image['before']
        if image['table']!='wb_finance_weekly_sku_aggregates' or old is None:continue
        if (old['seller_id'],old['week_start'],old['week_end'])!=(sync['seller_id'],sync['week_start'],sync['week_end']):continue
        nm=str(old['nm_id']);rid='SKU:'+nm+'|'+METRIC;value=amounts.get(rid)
        if value in ('',None) or Decimal(str(value))==0:continue
        require(image['key']=={k:old[k] for k in ('seller_id','week_start','week_end','nm_id')},'undo-key')
        require(nm in currents and nm in source['observations'][STALE_DAY]['skus'],'sku-scope')
        new=currents[nm]
        require(old['week_content_hash']==new['week_content_hash']==sync['content_hash'],'raw-week-hash-drift')
        require(old['formula_version']==new['formula_version']=='wb_finance_weekly_sku_aggregate_v5','formula-version')
        require(json.loads(old['metrics_json'])['profit_method_version']=='wb_finance_profit_covered_revenue_v4_signed_deductions'
            and json.loads(new['metrics_json'])['profit_method_version']=='wb_finance_profit_full_revenue_v5_explicit_estimates','policy-version-linkage')
        daily=[d for d in json.loads(old['coverage_json'])['daily_rows'] if d['operation_date']==STALE_DAY]
        require(len(daily)==1 and Decimal(str(value))==Decimal(str(daily[0]['uncovered_sales_revenue_rub'])),'prior-value-mismatch')
        require(not plan.get('metadata',{}).get('server_cell_presentation',{}).get(rid,{}).get(STALE_DAY),'intervening-cell-provenance')
        require(rid not in cells,'duplicate-prior-sku')
        cells[rid]={'before':value,'prior_sequence_no':image['sequence_no'],'prior_row_digest':digest(old),
            'raw_week_hash':old['week_content_hash'],'after':source['observations'][STALE_DAY]['skus'][nm]}
    require(len(cells)==62,'exact-62-scope-required')
    require({rid for rid,v in amounts.items() if v not in ('',None) and Decimal(str(v))!=0}==set(cells),'unproven-populated-cell')
    return {'mode':STALE_MODE,'date':STALE_DAY,'recovery':op,'artifact':artifact,'receipt':receipt,
        'undo_images':images,'ready_snapshot_id':record['snapshot_id'],'ready_plan_digest':digest(plan),
        'current_source_sha256':digest(source),'cells':cells}


def project(plan, *, source, operation_id, stale=None):
    result = deepcopy(plan); sheet = data_sheet(result); changes=[]
    presentation = result.setdefault('metadata',{}).setdefault('server_cell_presentation',{})
    for day, ob in source['observations'].items():
        if day not in sheet['header']: continue
        index = sheet['header'].index(day)
        target_ids = {'SKU:'+nm+'|'+METRIC for nm in ob['skus']} | {'TOTAL|total_'+METRIC}
        row_ids = [r[1] for r in sheet['rows']]
        if any(row_ids.count(rid)!=1 for rid in target_ids):
            raise ValueError('finance-absence-target-row-missing-or-duplicate')
        for row in sheet['rows']:
            scope, metric = row[1].split('|',1)
            if scope=='TOTAL' and metric=='total_'+METRIC:
                value=ob['total']; kind='complete_roster_daily_finance'
            elif scope.startswith('SKU:') and metric==METRIC and scope[4:] in ob['skus']:
                value=ob['skus'][scope[4:]]; kind=ob['kinds'][scope[4:]]
            else: continue
            consistent_existing=False
            if row[index] not in ('',None):
                if isinstance(row[index],bool) or Decimal(str(row[index]))!=Decimal(value):
                    proof=(stale or {}).get('cells',{}).get(row[1]) if day==STALE_DAY else None
                    if (not proof or plan.get('as_of_date')!=STALE_DAY or digest(plan)!=stale['ready_plan_digest']
                            or proof['before']!=row[index] or Decimal(proof['after'])!=Decimal(value)):
                        raise ValueError('finance-absence-populated-value-conflicts-with-source')
                else:
                    consistent_existing=True
            cell={'source':'complete_week_finance_diagnostic_v1','source_as_of_date':day,
                'source_digest':source['snapshot_id'],'week_proof':ob['week_proof'],
                'diagnostic_evidence_kind':kind,'state':'confirmed','tone':'neutral',
                'quality_state':'confirmed','quality_label':'По финансовому отчёту WB',
                'management_value':value,
                'reason':('Отсутствие операций подтверждено полным финансовым отчётом WB.'
                    if kind=='complete_week_no_operation' else
                    'Продажи без себестоимости по дневным данным полного финансового отчёта WB.'),
                'operation_id':operation_id}
            if kind != 'complete_week_no_operation':
                cell.update(quality_label='По политике себестоимости WebCore',
                    reason='Диагностика по операциям финансового отчёта WB и принятой политике себестоимости WebCore, включая одобренные оценки. Ноль означает отсутствие продаж без принятой себестоимости, а не отсутствие продаж или подтверждение точной себестоимости.',
                    cost_basis='webcore_accepted_cost_policy_including_approved_estimates',
                    certifies_exact_cost=False)
            if stale and row[1] in stale['cells'] and day==STALE_DAY and row[index] not in ('',None):
                cell['prior_projection']={'recovery_operation':STALE_RECOVERY,'proof_sha256':digest(stale),
                    'ready_plan_digest':stale['ready_plan_digest'],**stale['cells'][row[1]]}
            if consistent_existing:
                old=presentation.get(row[1],{}).get(day)
                if old:
                    # Preserve accepted provenance. Only an already identical
                    # source-owned presentation is an idempotent no-op.
                    ignored={'operation_id','prior_projection'}
                    if ({k:v for k,v in old.items() if k not in ignored}
                            =={k:v for k,v in cell.items() if k not in ignored}):
                        continue
                    raise ValueError('finance-absence-existing-presentation-conflict')
                # Explicitly preserve the stored numeric representation too.
                value=row[index]
            changes.append({'date':day,'row_id':row[1],'before':row[index],'after':value,'provenance':cell})
            row[index]=value; presentation.setdefault(row[1],{})[day]=cell
    if non_target_digest(plan,changes)!=non_target_digest(result,changes):
        raise ValueError('finance-absence-nontarget-changed')
    return result,changes

class FinanceAbsencePublicationAdapter(WebVitrinaManagementHistoryAdapter):
    def apply(self, request, operation_id, preview):
        runtime, _ = self.target(request)
        try:
            return super().apply(request, operation_id, preview)
        except Exception:
            if (runtime / 'evidence' / (operation_id + '.before.json')).exists():
                raise AmbiguousSubmit('finance-absence-readback-required') from None
            raise

    def readback(self, request, operation_id):
        result = super().readback(request, operation_id)
        if result['state'] != 'applied':
            return result
        runtime, db = self.target(request)
        candidate = json.loads((runtime / 'evidence' / (operation_id + '.before.json')).read_text())['candidate']
        with readonly(db) as conn:
            for update in candidate['updates']:
                row = conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                                   (update['bundle_version'], update['as_of_date'])).fetchone()
                if row is None or digest(json.loads(row[0])) != digest(json.loads(update['after_plan_json'])):
                    result['state'] = 'ambiguous'
                    result['mismatches'].append('exact-plan-or-provenance-drift:' + update['as_of_date'])
        return result

    def build(self, request, operation_id, conn):
        source=request['source']
        if request['dates']!=source['dates'] or digest(source)!=request['source_sha256']:
            raise ValueError('finance-absence-source-binding-invalid')
        active=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
        if not active or active[0]!=source['bundle_version']:
            raise ValueError('finance-absence-bundle-drift')
        mode=request.get('mode','missing_only')
        if mode not in {'missing_only',STALE_MODE} or (mode=='missing_only' and 'stale_source' in request):
            raise ValueError('finance-absence-mode-invalid')
        runtime,db=self.target(request)
        caller=next((Path(r[2]).resolve() for r in conn.execute('PRAGMA database_list') if r[1]=='main' and r[2]),None)
        if caller!=db.resolve(): raise ValueError('finance-absence-caller-target-mismatch')
        with readonly(db) as source_conn:
            source_conn.execute('BEGIN')
            fresh=capture_source(source_conn,dates=request['dates'],bundle_version=source['bundle_version'],nm_ids=source['nm_ids'])
            stale=None
            if mode==STALE_MODE:
                if STALE_DAY not in request.get('ready_dates',[]):raise ValueError('finance-stale-ready-date-required')
                stale=capture_stale_source(source_conn,runtime=runtime,source=fresh)
                if stale!=request.get('stale_source') or digest(stale)!=request.get('stale_source_sha256'):
                    raise ValueError('finance-stale-source-drift')
            if self.target(request)!=(runtime,db): raise ValueError('finance-absence-source-authority-drift')
        if digest(fresh)!=request['source_sha256']: raise ValueError('finance-absence-source-drift')
        ready_dates=request.get('ready_dates')
        if (not isinstance(ready_dates,list) or not 1<=len(ready_dates)<=4
                or any(not isinstance(d,str) for d in ready_dates)
                or ready_dates!=sorted(set(ready_dates))
                or any(date.fromisoformat(d).isoformat()!=d for d in ready_dates)):
            raise ValueError('finance-absence-ready-batch-invalid')
        marks = ','.join('?' for _ in ready_dates)
        records = [dict(r) for r in conn.execute(
            'SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? '
            'AND as_of_date IN ('+marks+') ORDER BY as_of_date',
            (source['bundle_version'], *ready_dates))]
        if ([r['as_of_date'] for r in records]!=ready_dates
                or sum(len(r['plan_json']) for r in records)>64*1024*1024):
            raise ValueError('finance-absence-ready-scope-invalid')
        updates, changes, before = [], [], []
        seen = set()
        for record in records:
            plan = json.loads(record['plan_json'])
            roster = sorted({int(r[1].split('|')[0][4:]) for r in data_sheet(plan)['rows'] if r[1].startswith('SKU:')})
            if roster != source['nm_ids']:
                raise ValueError('finance-absence-ready-roster-mismatch')
            matched=set(plan['date_columns']) & set(request['dates'])
            if not matched:
                raise ValueError('finance-absence-ready-no-target-dates')
            seen.update(matched)
            after, delta = project(plan, source=source, operation_id=operation_id, stale=stale if record['as_of_date']==STALE_DAY else None)
            if not delta:
                continue
            unchanged = non_target_digest(plan, delta)
            updates.append({'bundle_version': record['bundle_version'], 'as_of_date': record['as_of_date'],
                'snapshot_id': record['snapshot_id'], 'after_plan_json': json.dumps(after, ensure_ascii=False, separators=(',', ':')),
                'non_target_before': unchanged, 'non_target_after': unchanged})
            before.append(record)
            changes.extend({**c, 'snapshot_id': record['snapshot_id'], 'outer_date': record['as_of_date']} for c in delta)
        if seen != set(request['dates']):
            raise ValueError('finance-absence-ready-day-missing')
        return {'updates': updates, 'changes': changes, 'remaining': [], 'before_images': before,
            'ready_dates':ready_dates, 'matched_dates':sorted(seen), 'prestate_sha256': digest(records), 'source_sha256': request['source_sha256'], 'operation_id': operation_id}
