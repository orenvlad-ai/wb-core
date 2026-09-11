"""Replay retained official Ads observations without inventing HTTP provenance."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.application.ads_compact_block import transform_legacy_payload
from packages.application.metric_completeness import ADS_ECONOMICS, ads_dependencies, ads_partial_presentation
from packages.application.vitrina_economics import project_catalog_economics
from packages.contracts.ads_daily_report import COUNTS, FIELDS, checked_ads_count, validate_ads_campaign_batch
from packages.contracts.source_attempt_diagnostics import source_digest


def assemble(source, nm_ids):
    day = source['date']
    if day < '2026-09-11' or datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError('ads-partial-date-outside-contract')
    catalog = source['catalog']
    expected = HttpBackedAdsCompactSource(complete_catalog=True)._extract_non_archived_advert_ids(catalog['payload'], snapshot_date=day)
    if catalog['path'] != '/adv/v1/promotion/count' or catalog['status'] != 200 or source_digest(catalog['payload']) != catalog['payload_digest']:
        raise ValueError('ads-catalog-observation-invalid')
    seen, observed, batches, sums, platforms = set(), set(), [], {}, set()
    observations = source['observations']
    if not 1 <= len(observations) <= 16 or not expected:
        raise ValueError('ads-observation-budget-invalid')
    for item in observations:
        url = urlparse(item['path']);query = parse_qs(url.query)
        ids = [int(v) for v in query.get('ids', [''])[0].split(',')]
        if (url.path != '/adv/v3/fullstats' or url.scheme or url.netloc or item['status'] != 200
                or query.get('beginDate') != [day] or query.get('endDate') != [day]
                or set(query) != {'ids', 'beginDate', 'endDate'} or not 1 <= len(ids) <= 50
                or len(set(ids)) != len(ids) or any(i <= 0 for i in ids) or seen.intersection(ids)
                or not set(ids).issubset(expected)):
            raise ValueError('ads-observation-request-binding-invalid')
        seen.update(ids)
        start, finish = (datetime.fromisoformat(item[k].replace('Z', '+00:00')) for k in ('started_at', 'finished_at'))
        if start.tzinfo is None or finish.tzinfo is None or finish < start:
            raise ValueError('ads-observation-clock-invalid')
        payload = item['payload']
        if source_digest(payload) != item['payload_digest']:
            raise ValueError('ads-observation-payload-drift')
        records = [] if payload is None else payload
        if not isinstance(records, list) or any(not isinstance(a, dict) for a in records):
            raise ValueError('ads-observation-payload-invalid')
        returned = [a.get('advertId') for a in records]
        if any(type(a) is not int for a in returned) or not set(returned).issubset(ids) or len(set(returned)) != len(returned):
            raise ValueError('ads-observation-response-binding-invalid')
        validate_ads_campaign_batch(records, campaign_ids=returned, snapshot_date=day, allow_unclassified_platform=True)
        observed.update(returned)
        batches.append({k: item[k] for k in ('path', 'started_at', 'finished_at', 'payload_digest') } | {
            'expected_campaign_ids': ids, 'returned_campaign_ids': returned,
            'missing_campaign_ids': sorted(set(ids)-set(returned)),
            'response_kind': 'null' if payload is None else 'list', 'status': 200})
        for campaign in records:
            for d in campaign['days']:
                for app in d['apps']:
                    if app['appType'] == 0: platforms.add(campaign['advertId'])
                    for row in app['nms']:
                        n = row['nmId']
                        if n not in nm_ids: continue
                        totals = sums.setdefault(n, {f: 0 if f in COUNTS else Decimal(0) for f in FIELDS})
                        for f in FIELDS:
                            totals[f] = checked_ads_count(totals[f]+row[f]) if f in COUNTS else totals[f]+Decimal(str(row[f]))
    if seen != set(expected) or not sums:
        raise ValueError('ads-observation-scope-incomplete')
    diagnostics = {'schema_version': 'source_attempt_diagnostics_v1', 'source_key': 'ads_compact',
        'source_date': day, 'source_observed_at': max(observations, key=lambda item:datetime.fromisoformat(item['finished_at'].replace('Z','+00:00')))['finished_at'],
        'source_digest': source_digest(source), 'counter_basis': 'retained_provider_responses',
        'attempt_kind': 'retained_evidence_assembly', 'attempt_status': 'returned',
        'partial_observation_contract': 'ads_partial_observed_v1', 'completeness_state': 'partial',
        'dated_roster_state': 'unqualified', 'expected_campaign_ids': sorted(expected),
        'observed_campaign_ids': sorted(observed), 'returned_campaign_ids': sorted(observed),
        'unresolved_campaign_ids': sorted(set(expected)-observed), 'missing_campaign_ids': sorted(set(expected)-observed),
        'unclassified_platform_campaign_ids': sorted(platforms), 'batches': batches,
        'catalog_observation': {k: v for k, v in catalog.items() if k != 'payload'},
        'zero_fill_applied': False, 'missing_sku_count': None, 'affected_nm_ids': None,
        'impact_scope_state': 'unknown_campaign_attribution_and_dated_roster'}
    rows = [{'nmId': n, 'snapshot_date': day, 'fetched_at': diagnostics['source_observed_at'],
             **{'ads_'+f: float(v) if isinstance(v, Decimal) else v for f,v in values.items()}} for n,values in sorted(sums.items())]
    return transform_legacy_payload({'snapshot_date':day,'requested_nm_ids':nm_ids,'source':diagnostics,'data':{'rows':rows}}).result


def project(plan, *, result, config, metrics, formulas, parameters, operation_id):
    from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator
    working = deepcopy(plan); day = result.snapshot_date
    sheet = next(s for s in working['sheets'] if s['sheet_name'] == 'DATA_VITRINA')
    index = sheet['header'].index(day); rows = {r[1]: r for r in sheet['rows']}
    slot = SimpleNamespace(slot_key='retained_ads', column_date=day)
    status = SimpleNamespace(source_key='ads_compact', kind='incomplete', temporal_slot=slot.slot_key,
                             column_date=day, diagnostics=result.diagnostics)
    evaluator = _MetricEvaluator(enabled_config=config, metrics_by_key=metrics, formulas_by_id=formulas,
                                 live_sources=SimpleNamespace(statuses=[status]))
    lookup = {i.nm_id:i for i in result.items}
    def direct(key, nm, _slot):
        if key.startswith('ads_'): return getattr(lookup.get(nm), key, None)
        row = rows.get(f'SKU:{nm}|{key}')
        cell = working.get('metadata', {}).get('server_cell_presentation', {}).get(f'SKU:{nm}|{key}', {}).get(day, {})
        if cell.get('state') == 'unavailable' or cell.get('candidate_only') is True or cell.get('source_as_of_date') not in (None, '', day):
            return None
        value = row[index] if row else None
        return value if type(value) in (int, float) else None
    evaluator._resolve_direct_sku = direct
    affected = ads_dependencies(metrics, formulas)
    for row in sheet['rows']:
        scope, key = row[1].split('|', 1)
        if key not in affected or key in ADS_ECONOMICS: continue
        if scope.startswith('SKU:'): value = evaluator.resolve_sku(key, int(scope[4:]), slot.slot_key)
        elif scope == 'TOTAL': value = evaluator.resolve_total(key, slot.slot_key)
        else: value = evaluator.resolve_group(key, scope[6:], slot.slot_key)
        row[index] = value if value is not None else ''
    cells = working['metadata'].setdefault('server_cell_presentation', {})
    for key, by_date in ads_partial_presentation(rows=sheet['rows'], slots=[slot], statuses=[status], metrics=metrics, formulas=formulas).items():
        cells.setdefault(key, {}).update(by_date)
    working = project_catalog_economics(working, day=day, parameters=parameters)
    slots_by_key = {s['slot_key']:s['column_date'] for s in working.get('temporal_slots', [])}
    for status_sheet in working['sheets']:
        if status_sheet['sheet_name'] != 'STATUS': continue
        for row in status_sheet['rows']:
            old = dict(zip(status_sheet['header'], row))
            source_key = str(old.get('source_key', ''))
            temporal_key = source_key.partition('[')[2].removesuffix(']')
            if not source_key.startswith('ads_compact[') or slots_by_key.get(temporal_key) != day: continue
            latest = dict(old, kind='incomplete', freshness=day, snapshot_date=day,
                requested_count=result.requested_count, covered_count=result.covered_count,
                missing_nm_ids=','.join(map(str,result.missing_nm_ids)),
                note=json.dumps({'resolution_rule':'accepted_partial_retained_observation',
                    'source_observed_at':result.diagnostics['source_observed_at'],
                    'observed_campaign_count':len(result.diagnostics['observed_campaign_ids']),
                    'unknown_campaign_ids':result.diagnostics['unresolved_campaign_ids'],
                    'dated_roster_state':'unqualified','missing_sku_count':None,
                    'source_digest':result.diagnostics['source_digest']},ensure_ascii=False,sort_keys=True))
            row[:] = [latest.get(k,'') for k in status_sheet['header']]
    working['metadata'].setdefault('ads_partial_publications', {})[day] = {
        'operation_id': operation_id, 'source_digest': result.diagnostics['source_digest'],
        'source_observed_at': result.diagnostics['source_observed_at'], 'kind': 'incomplete'}
    return working
