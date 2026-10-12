"""Source-free completion of a retained current column after its day closes.

Only exact dated Ready rows whose target is still a current slot are eligible.
Accepted source bytes and the original Ready are pinned before enrollment.
The existing cycle owns publication and the native History acknowledgement.
"""
from copy import deepcopy
from dataclasses import replace
import json

from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.ready_publication import canonical, digest, readonly

SOURCES = ('sales_funnel_history', 'fin_report_daily')
MAX_READY_BYTES = 64 * 1024**2
DEPENDENT_METRICS = {'ads_drr', 'ads_drr_total'}


def retained_metric_keys(keys, day):
    from packages.application.registry_upload_http_entrypoint import WEB_VITRINA_SOURCE_METRIC_KEYS
    from packages.application.sheet_vitrina_v1_weighted_seller_price import weighted_price_source, WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY
    metrics = {metric for key in keys for metric in WEB_VITRINA_SOURCE_METRIC_KEYS[key]}
    if weighted_price_source(day) in keys:
        metrics.add(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY)
    return sorted(metrics)


def retained_candidates(backlog, *, yesterday):
    """Bounded metadata discovery, then validate only selected exact sources."""
    runtime = backlog.runtime
    bundle = runtime.load_current_state().bundle_version
    # Bound identities before any JSON work. Close each SQLite reader before
    # parsing so an archive cannot hold a long shared lock in DELETE mode.
    with readonly(runtime.db_path) as conn:
        rows = conn.execute("""SELECT as_of_date,length(CAST(plan_json AS BLOB)) AS size
            FROM sheet_vitrina_v1_ready_snapshots
            WHERE bundle_version=? AND as_of_date<?
            ORDER BY as_of_date DESC LIMIT 32""", (bundle, yesterday)).fetchall()
    result = {}
    current = runtime.load_current_state()
    from packages.application.vitrina_economics import EFFECTIVE_DATE
    enabled = (live.reporting_config(runtime.db_path, current.config_v2)[0]
        if live.current_business_date_iso(backlog.sources.block.now_factory()) >= EFFECTIVE_DATE
        else [v for v in current.config_v2 if v.enabled])
    for row in rows:
        if row['size'] > MAX_READY_BYTES:
            continue
        day = row['as_of_date']
        with readonly(runtime.db_path) as conn:
            value = conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots '
                'WHERE bundle_version=? AND as_of_date=? AND length(CAST(plan_json AS BLOB))<=?',
                (bundle, day, MAX_READY_BYTES)).fetchone()
        if value is None:
            continue
        encoded = value[0]; plan = json.loads(encoded)
        if not any(slot.get('column_date') == day and slot.get('slot_key') == live.TEMPORAL_SLOT_TODAY_CURRENT
                for slot in plan.get('temporal_slots', [])):
            continue
        consumed = plan.get('metadata', {}).get('publication_inputs', {}).get('consumed', {})
        anchors = {}
        for key in SOURCES:
            anchor = backlog._valid_anchor(key, day, [v.nm_id for v in enabled])
            if anchor:
                anchors[key] = anchor
        # A current column becomes closed once. Require both canonical
        # families before replacement so a later family cannot be stranded.
        if (set(anchors) != set(SOURCES) or all(
                consumed.get(canonical([key, day, anchor['snapshot_role']])) == anchor
                for key, anchor in anchors.items())):
            continue
        result[day] = dict(before_ready_digest=digest(encoded), anchors=anchors)
        if len(result) == 2:
            break
    return result


def check_original_ready(backlog, day, item):
    from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklogConflict
    value = backlog.status()
    with readonly(backlog.runtime.db_path) as conn:
        row = conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots '
            'WHERE bundle_version=? AND as_of_date=? AND length(CAST(plan_json AS BLOB))<=?',
            (value['bundle_version'], day, MAX_READY_BYTES)).fetchone()
        if row is None or digest(row[0]) != item['retained_replay']['before_ready_digest']:
            raise ClosedBacklogConflict('closed_retained_ready_changed:' + day)
        for key, source in item['sources'].items():
            if backlog._anchor(key, day, connection=conn) != source['anchor']:
                raise ClosedBacklogConflict('closed_accepted_source_changed:' + key)
    return row[0]


def merge_retained_plan(backlog, day, partial):
    """Use the canonical evaluator/merge and dated Proxy projection only."""
    from packages.application.registry_upload_db_backed_runtime import (
        _deserialize_sheet_vitrina_plan, _serialize_sheet_vitrina_plan)
    from packages.application.registry_upload_http_entrypoint import _merge_source_group_ready_snapshot
    from packages.application.web_vitrina_management_history import dated_parameters, recalculate_dated_proxy
    value = backlog.status(); item = value['dates'][day]
    original = _deserialize_sheet_vitrina_plan(check_original_ready(backlog, day, item))
    keys = sorted(item['sources'])
    metrics = retained_metric_keys(keys, day)
    stamp = backlog.sources.block._diagnostic_timestamp()
    merged, summary = _merge_source_group_ready_snapshot(previous_plan=original,
        partial_plan=partial, source_group_id='wb_api', source_keys=keys, metric_keys=metrics,
        refreshed_at=stamp, previous_refreshed_at='', selected_as_of_date=day,
        business_date=live.current_business_date_iso(backlog.sources.block.now_factory()))
    # The promoted Ready used to contain the donor's previous date as well.
    # Its exact replacement owns this date only; the donor Ready is untouched.
    sheets = []
    for sheet in merged.sheets:
        if sheet.sheet_name == 'DATA_VITRINA':
            index = sheet.header.index(day)
            rows = [list(row[:2]) + [deepcopy(row[index])] for row in sheet.rows]
            sheet = replace(sheet, header=list(sheet.header[:2]) + [day], rows=rows,
                column_count=3, write_rect=f'A1:C{len(rows)+1}')
        elif sheet.sheet_name == 'STATUS':
            # A partial replay must not replace the donor date's status with
            # this date's status under the same yesterday_closed slot label.
            selected = [list(row) for row in next(s for s in partial.sheets if s.sheet_name == 'STATUS').rows
                if row and str(row[0]).split('[')[0] in keys and '[yesterday_closed]' in str(row[0])]
            retained = []
            for row in next(s for s in original.sheets if s.sheet_name == 'STATUS').rows:
                if row and str(row[0]).split('[')[0] not in keys and '[today_current]' in str(row[0]):
                    copy = list(row); copy[0] = str(copy[0]).replace('[today_current]', '[yesterday_closed]')
                    retained.append(copy)
            selected = retained + selected
            sheet = replace(sheet, rows=selected, row_count=len(selected),
                write_rect=f'A1:K{len(selected)+1}')
        sheets.append(sheet)
    metadata = deepcopy(merged.metadata)
    cells = metadata.setdefault('server_cell_presentation', {})
    for row_id in summary['updated_row_ids']:
        if row_id == 'TOTAL|weighted_price_seller_discounted':
            continue  # Canonical merge already carries its exact dated scope.
        old = deepcopy(cells.get(row_id, {}).get(day, {}))
        cells.setdefault(row_id, {}).pop(day, None)
        fresh = partial.metadata.get('server_cell_presentation', {}).get(row_id, {}).get(day)
        if fresh is not None:
            cells[row_id][day] = deepcopy(fresh)
        evidence = old.get('metric_scope_evidence', {})
        if evidence.get('operand_date') == day and isinstance(evidence.get('applicable_scope'), list):
            cells[row_id].setdefault(day, {})['metric_scope_evidence'] = {**evidence, 'missing_scope': []}
    metadata['local_derive_expected_ready_fingerprint'] = partial.metadata['local_derive_expected_ready_fingerprint']
    metadata['closed_retained_source_replay'] = dict(date=day, sources=keys,
        before_ready_digest=item['retained_replay']['before_ready_digest'])
    merged = replace(merged, date_columns=[day], temporal_slots=[live.SheetVitrinaV1TemporalSlot(
        slot_key=live.TEMPORAL_SLOT_YESTERDAY_CLOSED, slot_label=live.TEMPORAL_SLOT_YESTERDAY_CLOSED,
        column_date=day)], sheets=sheets, metadata=metadata)
    merged = recalculate_retained_dependencies(merged, runtime=backlog.runtime, day=day)
    with readonly(backlog.runtime.db_path) as conn:
        payload = recalculate_dated_proxy(json.loads(_serialize_sheet_vitrina_plan(merged)), day=day,
            parameters=dated_parameters(conn, day), operation_id='closed-retained:' + day)['plan']
    data = next(sheet for sheet in payload['sheets'] if sheet['sheet_name'] == 'DATA_VITRINA')
    data['write_rect'] = f"A1:C{len(data['rows'])+1}"
    return _deserialize_sheet_vitrina_plan(canonical(payload))


def validate_retained_candidate(plan, expected, *, business_date, runtime):
    """Admit only the bounded dated replacement; no caller boolean is proof."""
    from packages.application.ready_publication import ReadyPublicationConflict
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    from packages.application.vitrina_economics import METRICS, TOTALS
    def require(condition):
        if not condition:
            raise ReadyPublicationConflict('closed_retained_candidate_invalid')
    marker = plan.metadata.get('closed_retained_source_replay', {})
    day = plan.as_of_date; keys = marker.get('sources', [])
    require(bool(expected.plan_json) and expected.as_of_date == day and day < business_date)
    require(isinstance(keys, list) and bool(keys) and sorted(set(keys)) == keys and set(keys) == set(SOURCES))
    require(marker == dict(date=day, sources=keys, before_ready_digest=digest(expected.plan_json)))
    original = _deserialize_sheet_vitrina_plan(expected.plan_json)
    require(original.as_of_date == day and any(s.column_date == day and s.slot_key == live.TEMPORAL_SLOT_TODAY_CURRENT for s in original.temporal_slots))
    # Exact dated bindings win over covering/default Ready in the native
    # selector. Do not remove the sole available column for another date.
    for other in set(original.date_columns) - {day}:
        with readonly(runtime.db_path) as conn:
            donor = conn.execute("""SELECT s.bundle_version,length(CAST(s.plan_json AS BLOB))
                FROM sheet_vitrina_v1_ready_snapshots s
                JOIN sheet_vitrina_v1_ready_revisions r USING(bundle_version,as_of_date)
                WHERE s.as_of_date=? ORDER BY s.activated_at DESC,s.refreshed_at DESC,
                s.as_of_date DESC,s.bundle_version DESC LIMIT 1""", (other,)).fetchone()
            require(donor is not None and donor[1] <= MAX_READY_BYTES)
            raw = conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?', (donor[0], other)).fetchone()[0]
        bound = _deserialize_sheet_vitrina_plan(raw)
        require(other in bound.date_columns)
    require(plan.date_columns == [day] and len(plan.temporal_slots) == 1
        and plan.temporal_slots[0].column_date == day and plan.temporal_slots[0].slot_key == live.TEMPORAL_SLOT_YESTERDAY_CLOSED)
    require(plan.plan_version == original.plan_version and plan.source_temporal_policies == original.source_temporal_policies)
    require(plan.metadata.get('local_derive_expected_ready_fingerprint') == expected.fingerprint)
    inputs = plan.metadata.get('publication_inputs', {})
    for key in keys:
        proof = inputs.get('consumed', {}).get(canonical([key, day, live.TEMPORAL_ROLE_ACCEPTED_CLOSED]), {})
        require(proof.get('source_key') == key and proof.get('snapshot_date') == day
            and proof.get('snapshot_role') == live.TEMPORAL_ROLE_ACCEPTED_CLOSED and bool(proof.get('digest')))
    allowed = set(retained_metric_keys(keys, day)) | set(METRICS) | set(TOTALS) | DEPENDENT_METRICS
    before = {s.sheet_name: s for s in original.sheets}; after = {s.sheet_name: s for s in plan.sheets}
    require(set(before) == set(after))
    for name in before:
        if name not in {'DATA_VITRINA', 'STATUS'}:
            require(before[name] == after[name])
    old, new = before['DATA_VITRINA'], after['DATA_VITRINA']; ix = old.header.index(day)
    require(new.header == list(old.header[:2]) + [day])
    old_rows = {r[1]: r for r in old.rows}; new_rows = {r[1]: r for r in new.rows}
    require(len(new_rows) == len(new.rows) and set(old_rows) <= set(new_rows))
    for identity, row in new_rows.items():
        require(len(row) == 3)
        if identity in old_rows:
            require(row[:2] == old_rows[identity][:2])
        if identity.split('|')[-1] not in allowed:
            require(identity in old_rows and canonical(row[2]) == canonical(old_rows[identity][ix]))
    # Only the selected sources' status rows change. Other observed source
    # statuses retain every byte except the slot label's dated normalization.
    retained = []
    for row in before['STATUS'].rows:
        if row and str(row[0]).split('[')[0] not in keys and '[today_current]' in str(row[0]):
            copy = list(row); copy[0] = str(copy[0]).replace('[today_current]', '[yesterday_closed]'); retained.append(copy)
    actual = [r for r in after['STATUS'].rows if str(r[0]).split('[')[0] not in keys]
    require(actual == retained)
    selected = [r for r in after['STATUS'].rows if str(r[0]).split('[')[0] in keys]
    require(len(selected) == len(keys) and {r[0] for r in selected} == {key + '[yesterday_closed]' for key in keys})
    require(all(r[1] == 'success' and day in r[2:7] for r in selected))
    before_meta = deepcopy(original.metadata); after_meta = deepcopy(plan.metadata)
    mutable = {'publication_inputs', 'local_derive_expected_ready_fingerprint', 'closed_retained_source_replay',
        'row_last_updated_at_by_row_id', 'source_group_last_updated_at', 'last_partial_group_refresh',
        'weighted_seller_price_formula', 'weighted_seller_price_history_preserved_dates'}
    for metadata in (before_meta, after_meta):
        for key in mutable: metadata.pop(key, None)
        for key in ('catalog_economics_coverage', 'daily_trading_pool'):
            dated = metadata.get(key, {})
            dated.pop(day, None)
            if not dated: metadata.pop(key, None)
        cells = metadata.get('server_cell_presentation', {})
        for identity in list(cells):
            if identity.split('|')[-1] in allowed:
                cells[identity].pop(day, None)
                if not cells[identity]: cells.pop(identity)
    require(canonical(before_meta) == canonical(after_meta))


def recalculate_retained_dependencies(plan, *, runtime, day):
    """Native DRR formula from accepted same-date SKU operands and coverage."""
    from types import SimpleNamespace
    from packages.application.web_vitrina_group_blocks import AcceptedGroupEvaluator, accepted_source_statuses, input_incomplete
    from packages.application.metric_completeness import aggregate_counters
    state = runtime.load_current_state()
    metadata = deepcopy(plan.metadata); cells = metadata.setdefault('server_cell_presentation', {})
    sheet = next(s for s in plan.sheets if s.sheet_name == 'DATA_VITRINA')
    rows = deepcopy(sheet.rows)
    accepted = []
    for row in rows:
        scope, key = row[1].split('|', 1)
        if scope.startswith('SKU:'):
            accepted.append(SimpleNamespace(nm_id=int(scope[4:]), scope_kind='SKU', metric_key=key,
                values_by_date={day: row[2]}, presentation_by_date={day: cells.get(row[1], {}).get(day, {})}))
    class Evaluator(AcceptedGroupEvaluator):
        def resolve_sku(self, key, nm_id, slot):
            if key == 'ads_drr':
                return live._MetricEvaluator.resolve_sku(self, key, nm_id, slot)
            return super().resolve_sku(key, nm_id, slot)
    # The accepted envelope is the reporting roster for this replay. A newly
    # added catalog row is not dated evidence of membership on this old day.
    ids = {r.nm_id for r in accepted}
    config = [c for c in live.reporting_config(runtime.db_path, state.config_v2)[0] if c.nm_id in ids]
    evaluator = Evaluator(rows=accepted, enabled_config=config,
        metrics_by_key={m.metric_key:m for m in state.metrics_v2},
        formulas_by_id={f.formula_id:f for f in state.formulas_v2},
        live_sources=live.TemporalLiveSources([], [SimpleNamespace(**v) for v in accepted_source_statuses(plan, column_date=day)],
            {day:SimpleNamespace(column_date=day)}, {}))
    for row in rows:
        scope, key = row[1].split('|', 1)
        if key not in DEPENDENT_METRICS:
            continue
        evaluator.visited_operands.clear()
        value = evaluator.resolve_total(key, day) if scope == 'TOTAL' else evaluator.resolve_sku(key, int(scope[4:]), day)
        row[2] = '' if value is None else value
        cell = cells.setdefault(row[1], {}).setdefault(day, {})
        operands = [evaluator.accepted[k] for k in evaluator.visited_operands if k in evaluator.accepted]
        partial = evaluator._partial_ads_slot(day) or any(input_incomplete(r.presentation_by_date.get(day, {})) for r in operands)
        detail = {'operand_date': day, 'before_ready_digest': metadata['closed_retained_source_replay']['before_ready_digest'],
            'metrics': ['ads_sum', 'orderSum'], 'formula_id': evaluator.metrics_by_key[key].calc_ref}
        cell.update(source_as_of_date=day, retained_formula_replay=detail)
        if partial:
            cell.update(quality_state='partial', completeness_state='unknown_scope', missing_sku_count=None)
        elif value is None:
            cell.update(quality_state='missing')
        else:
            cell.pop('state', None)
            if cell.get('quality_state') in ('missing', 'unavailable'): cell.pop('quality_state', None)
        if value is None:
            cell['state'] = 'unavailable'
    # Re-evaluate existing dated scope evidence, without declaring today's
    # catalog to be the historical denominator or filling absent SKU with zero.
    from dataclasses import dataclass
    @dataclass
    class CounterRow:
        row_id: str
        scope_key: str
        scope_kind: str
        metric_key: str
        values_by_date: dict
        presentation_by_date: dict
    counter_rows = [CounterRow(r[1], r[1].split('|')[0], r[1].split(':')[0] if ':' in r[1].split('|')[0] else 'TOTAL',
        r[1].split('|')[1], {day:r[2]}, {day:cells.get(r[1], {}).get(day, {})}) for r in rows]
    for row in aggregate_counters(counter_rows, dates=[day]):
        if row.metric_key in set(retained_metric_keys(SOURCES, day)) | DEPENDENT_METRICS and day in row.presentation_by_date:
            cells.setdefault(row.row_id, {})[day] = row.presentation_by_date[day]
    return replace(plan, sheets=[replace(s, rows=rows) if s.sheet_name == 'DATA_VITRINA' else s for s in plan.sheets], metadata=metadata)
