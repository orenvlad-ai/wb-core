"""Publish missing FBS and physical WB quantities from exact dated observations.

No accounting book, inventory finalization, movement, cost or profit is changed.
The existing ready-plan adapter supplies CAS, before images and exact readback.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json

from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly
from apps.production_apply_contract import AmbiguousSubmit
from packages.application.web_vitrina_management_history import digest, data_sheet, non_target_digest
from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE, STOCK_RUNS_TABLE
from packages.application.web_vitrina_official_fbs import (
    SOURCE, FBS_TOTAL, FBS_FACILITY, add_official_wb_quantities, _official_estimated_cell,
)
from packages.application.official_wb_physical_read import read_official_wb_physical_snapshot
from packages.business_time import current_business_date_iso


def capture_source(conn, *, dates, bundle_version, nm_ids, now):
    """Choose a dated observation, never carry today's quantity into the past."""
    days = sorted(set(dates))
    today = current_business_date_iso(now)
    if not days or len(days) > 7 or any(date.fromisoformat(d).isoformat() != d or d > today for d in days):
        raise ValueError('fbs-quantity-date-scope-invalid')
    if not nm_ids or nm_ids != sorted(set(nm_ids)):
        raise ValueError('fbs-quantity-roster-invalid')
    runs = conn.execute(f"SELECT run_id,completed_at FROM {REGISTRY_RUNS_TABLE} "
                        "WHERE status='success' AND complete=1 AND completed_at>=? "
                        "ORDER BY run_sequence DESC LIMIT 512", ((date.fromisoformat(days[0]) - timedelta(days=1)).isoformat(),)).fetchall()
    selected = {}
    for run in runs:
        stamp = datetime.fromisoformat(run['completed_at'].replace('Z', '+00:00'))
        day = current_business_date_iso(stamp)
        if day not in days or day in selected:
            continue
        observations = conn.execute(f'SELECT snapshot_at FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=?',
                                    (run['run_id'],)).fetchall()
        if not observations:
            continue
        stamps = [datetime.fromisoformat(r['snapshot_at'].replace('Z', '+00:00')) for r in observations]
        if any(t.tzinfo is None or t > now or current_business_date_iso(t) != day for t in stamps):
            continue
        # Historical age is evaluated at the original observation, while today's
        # publication must still satisfy the actual current freshness policy.
        observation_now = now if day == today else max(stamps)
        stock = read_complete_official_fbs_stock(conn, universe=nm_ids, day=day,
            now=observation_now, generation_id=run['run_id'])
        selected[day] = {
            'generation_id': stock['generation_id'], 'generation_digest': stock['generation_digest'],
            'captured_at': stock['captured_at'], 'facility_evidence': stock['facility_evidence'],
            'facilities': stock['facilities'],
            'skus': {str(n): {f: str(q) for f, q in item['facilities'].items()}
                     for n, item in stock['skus'].items()},
            'wb_source': read_official_wb_physical_snapshot(conn, universe=nm_ids, day=day, now=now),
        }
    if set(selected) != set(days):
        raise ValueError('fbs-exact-dated-generation-missing')
    source = {'dates': days, 'bundle_version': bundle_version, 'nm_ids': nm_ids, 'observations': selected}
    return {**source, 'snapshot_id': 'official-fbs-quantity:' + digest(source)}


def project(plan, *, source, operation_id):
    result = deepcopy(plan)
    sheet = data_sheet(result)
    presentation = result.setdefault('metadata', {}).setdefault('server_cell_presentation', {})
    changes = []
    for day, observation in source['observations'].items():
        if day not in sheet['header']:
            continue
        index = sheet['header'].index(day)
        amounts = {int(n): {f: int(q) for f, q in values.items()} for n, values in observation['skus'].items()}
        estimate = {**observation, 'available': True, 'date': day, 'quantity_only': True,
            'source': SOURCE, 'functional_version_id': '',
            'skus': {n: {'facilities': v, 'fbs_quantity': sum(v.values())} for n, v in amounts.items()},
            'total': {'facilities': {f: sum(v[f] for v in amounts.values()) for f in observation['facilities']},
                      'fbs_quantity': sum(sum(v.values()) for v in amounts.values())}}
        estimate = add_official_wb_quantities(estimate, observation['wb_source'])
        for row in sheet['rows']:
            scope, metric = row[1].split('|', 1)
            key = metric.removeprefix('total_')
            if key not in {FBS_TOTAL, 'inventory_wb_total_qty_v1', 'stock_total'} and key not in {FBS_FACILITY + f for f in observation['facilities']}:
                continue
            if scope == 'TOTAL':
                item = estimate['total']
            elif scope.startswith('SKU:') and scope[4:].isdigit() and int(scope[4:]) in amounts:
                item = estimate['skus'][int(scope[4:])]
            else:
                continue
            value, cell = _official_estimated_cell(item, key=key, estimate=estimate, day=day)
            if value in ('', None):
                raise ValueError('official-quantity-component-missing')
            # A populated observation is outside this missing-only recovery mask.
            if row[index] not in ('', None):
                try:
                    consistent = not isinstance(row[index], bool) and Decimal(str(row[index])) == value
                except (InvalidOperation, ValueError):
                    consistent = False
                if not consistent:
                    raise ValueError('fbs-populated-quantity-conflicts-with-source')
                continue
            cell['operation_id'] = operation_id
            changes.append({'date': day, 'row_id': row[1], 'before': row[index], 'after': value, 'provenance': cell})
            row[index] = value
            presentation.setdefault(row[1], {})[day] = cell
    if non_target_digest(plan, changes) != non_target_digest(result, changes):
        raise ValueError('fbs-quantity-nontarget-changed')
    return result, changes


class FbsQuantityPublicationAdapter(WebVitrinaManagementHistoryAdapter):
    def apply(self, request, operation_id, preview):
        runtime, _ = self.target(request)
        try:
            return super().apply(request, operation_id, preview)
        except Exception:
            if (runtime / 'evidence' / (operation_id + '.before.json')).exists():
                raise AmbiguousSubmit('fbs-quantity-readback-required') from None
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
        source = request['source']
        if request['dates'] != source['dates'] or digest(source) != request['source_sha256']:
            raise ValueError('fbs-quantity-source-binding-invalid')
        active = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
        if not active or active[0] != source['bundle_version']:
            raise ValueError('fbs-quantity-bundle-drift')
        fresh = capture_source(conn, dates=request['dates'], bundle_version=source['bundle_version'],
                               nm_ids=source['nm_ids'], now=datetime.now(timezone.utc))
        if digest(fresh) != request['source_sha256']:
            raise ValueError('fbs-quantity-source-drift')
        marks = ','.join('?' for _ in request['dates'])
        records = [dict(r) for r in conn.execute(
            'SELECT * FROM sheet_vitrina_v1_ready_snapshots s WHERE bundle_version=? AND EXISTS '
            "(SELECT 1 FROM json_each(s.plan_json,'$.date_columns') d WHERE d.value IN (" + marks + ')) ORDER BY as_of_date',
            (source['bundle_version'], *request['dates']))]
        if not records or len(records) > 32 or sum(len(r['plan_json']) for r in records) > 64 * 1024 * 1024:
            raise ValueError('fbs-quantity-ready-scope-invalid')
        updates, changes, before = [], [], []
        seen = set()
        for record in records:
            plan = json.loads(record['plan_json'])
            roster = sorted({int(r[1].split('|')[0][4:]) for r in data_sheet(plan)['rows'] if r[1].startswith('SKU:')})
            if roster != source['nm_ids']:
                raise ValueError('fbs-quantity-ready-roster-mismatch')
            seen.update(set(plan['date_columns']) & set(request['dates']))
            after, delta = project(plan, source=source, operation_id=operation_id)
            if not delta:
                continue
            unchanged = non_target_digest(plan, delta)
            updates.append({'bundle_version': record['bundle_version'], 'as_of_date': record['as_of_date'],
                'snapshot_id': record['snapshot_id'], 'after_plan_json': json.dumps(after, ensure_ascii=False, separators=(',', ':')),
                'non_target_before': unchanged, 'non_target_after': unchanged})
            before.append(record)
            changes.extend({**c, 'snapshot_id': record['snapshot_id'], 'outer_date': record['as_of_date']} for c in delta)
        if seen != set(request['dates']):
            raise ValueError('fbs-quantity-ready-day-missing')
        return {'updates': updates, 'changes': changes, 'remaining': [], 'before_images': before,
            'prestate_sha256': digest(records), 'source_sha256': request['source_sha256'], 'operation_id': operation_id}
