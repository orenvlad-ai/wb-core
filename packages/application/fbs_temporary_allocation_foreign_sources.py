"""Bounded foreign-source reconstruction for the unfinished allocation suffix.

The dated WB edition remains the quantity authority. Late native documents
carry their immutable money; a completed supplier replay contributes only its
own dated production/China source records. No runtime, collector or writer is
constructed here, and no foreign whole-version snapshot is admitted.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import json
import re
import sqlite3

from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_stages as stages
from packages.application import warehouse_targeted_replay as targeted
from packages.application.fbs_historical_fbo_stage_sources import _Reader
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.warehouse_functional import FUNCTIONAL_CUTOVER_ID

CONTRACT = 'temporary_allocation_foreign_suffix_sources_v1'
P = 'sheet_vitrina_v1_'
MAX_BYTES = 64 * 1024**2
SUPPLIER_STAGES = {'production', 'china_to_ff'}
HEADER_SQL = f'''SELECT shipment_id,invoice_no,shipment_date,actual_shipment_date,
    actual_ff_acceptance_date,order_status,match_status,expenses_complete,updated_at
    FROM {P}supplier_shipments WHERE shipment_id=?'''
COST_QUERIES = {
    'lines': f'''SELECT line_id,line_type,sort_order,internal_nm_id,qty,unit_price,
        amount,match_status FROM {P}supplier_shipment_lines WHERE shipment_id=?
        ORDER BY sort_order,line_id''',
    'documents': f'''SELECT document_id,document_type,document_date,parse_status,
        parser_version,updated_at FROM {P}supplier_financial_documents
        WHERE supplier_order_id=? ORDER BY document_id''',
    'expenses': f'''SELECT line_id,financial_document_id,sort_order,category,
        amount,currency,amount_rub,vat_rate,vat_amount_rub,status,raw_json
        FROM {P}supplier_financial_expense_lines WHERE supplier_order_id=?
        ORDER BY financial_document_id,sort_order,line_id''',
    'cny_operations': f'''SELECT operation_id,operation_type,operation_date,sequence_key,
        cny_delta,rub_value_delta,source_document_id,updated_at
        FROM {P}cny_ledger_operations WHERE source_order_id=? ORDER BY sequence_key,operation_id''',
}


def require(value, code):
    if not value:
        raise ValueError('temporary_foreign_' + code)


def _number(value):
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, 'source_money_invalid')
    return result


def _timestamp(value):
    # Native timestamps are aware ISO seconds with at most microseconds. Do
    # not silently truncate a higher-precision or timezone-free source.
    require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value),
        'native_timestamp_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'native_timestamp_invalid')
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError('temporary_foreign_native_timestamp_invalid') from error


def _document_set(documents):
    result = {}
    for document in documents:
        identity = document['document_id']
        require(identity not in result or result[identity] == document, 'duplicate_document_conflict')
        result[identity] = deepcopy(document)
    return [result[k] for k in sorted(result)]


def _sources(rows, shipment, *, targeted_policy=False):
    result = []
    for row in rows:
        if row['warehouse_key'] not in SUPPLIER_STAGES:
            continue
        records = row['provenance'].get('source_records')
        require(isinstance(records, list), 'supplier_records_missing')
        if targeted_policy:
            # The existing targeted writer sums at native precision28 and
            # serializes the aggregate to six decimals. Source records retain
            # their original bytes; this validates that policy, never rounds
            # an output balance or substitutes a new monetary operand.
            with localcontext() as context:
                context.prec = 28
                q = targeted._text(sum((_number(r['flow_quantity']) for r in records), Decimal(0)))
                c = targeted._text(sum((_number(r['flow_capital_rub']) for r in records), Decimal(0)))
            conserved = _number(q) == _number(row['quantity']) and _number(c) == _number(row['capital_rub'])
        else:
            conserved = sum((_number(r['flow_quantity']) for r in records), Decimal(0)) == _number(row['quantity']) and sum((_number(r['flow_capital_rub']) for r in records), Decimal(0)) == _number(row['capital_rub'])
        require(conserved, 'supplier_records_not_conserved')
        for record in records:
            if record.get('shipment_id') == shipment:
                result.append(dict(stage=row['warehouse_key'], nm_id=int(row['nm_id']), source=deepcopy(record)))
    return result


def _foreign_sources(rows, shipment):
    return sorted((dict(stage=r['warehouse_key'], nm_id=int(r['nm_id']), source=source)
        for r in rows for source in r['provenance'].get('source_records', [])
        if source.get('shipment_id') != shipment), key=canonical)


def _capture_replay(reader, version_id):
    version = reader.one(f'SELECT * FROM {P}warehouse_functional_versions WHERE version_id=?', (version_id,))
    require(version['status'] == 'good' and version['version_kind'] == 'targeted_supplier_replay'
            and version['cutover_id'] == FUNCTIONAL_CUTOVER_ID, 'unsupported_replay')
    publication = reader.one(f'SELECT * FROM {targeted.TARGETED_PUBLICATION_TABLE} WHERE version_id=?', (version_id,))
    undo_row = reader.one(f'SELECT * FROM {targeted.TARGETED_UNDO_TABLE} WHERE publication_id=?', (publication['publication_id'],))
    require(publication['status'] == 'complete' and undo_row['status'] == 'ready'
            and undo_row['rolled_back_at'] is None, 'replay_not_complete_or_rolled_back')
    shipment = undo_row['shipment_id']
    before = json.loads(undo_row['before_header_json']); after = json.loads(undo_row['after_header_json'])
    rows_before = json.loads(undo_row['target_rows_before_json']); rows_after = json.loads(undo_row['target_rows_after_json'])
    undo = dict(shipment_id=shipment, before_header=before, after_header=after,
                base_version_id=undo_row['base_version_id'], published_version_id=undo_row['published_version_id'],
                target_rows_before=rows_before, target_rows_after=rows_after)
    require(targeted._fingerprint(undo) == undo_row['manifest_digest']
            and targeted._fingerprint(rows_before) == publication['target_before_digest']
            and targeted._fingerprint(rows_after) == publication['target_after_digest'], 'replay_manifest_changed')
    require(all(r['warehouse_key'] in SUPPLIER_STAGES for r in [*rows_before, *rows_after])
            and _foreign_sources(rows_before, shipment) == _foreign_sources(rows_after, shipment),
            'replay_changed_foreign_sources')
    watermark = json.loads(version['source_watermarks_json'])
    day = publication['earliest_business_date']
    require(publication['stable_source_id'] == 'supplier_shipment:' + shipment
            and undo_row['published_version_id'] == version_id
            and publication['base_version_id'] == undo_row['base_version_id'] == watermark.get('base_version_id')
            and publication['plan_fingerprint'] == version['plan_fingerprint']
            and watermark.get('stable_source_id') == publication['stable_source_id']
            and watermark.get('source_revision') == publication['source_revision']
            and watermark.get('earliest_business_date') == version['business_effective_date'] == day
            and publication['non_target_before_digest'] == publication['non_target_after_digest']
            and json.loads(publication['blocker_summary_json']) == [], 'replay_binding_changed')
    current = reader.one(HEADER_SQL, (shipment,))
    require({k: v for k, v in current.items() if k != 'updated_at'} ==
            {k: v for k, v in after.items() if k != 'updated_at'}, 'supplier_header_changed')
    # The publication hashes pre-mutation cost inputs plus the original header.
    # Only the proven factual-header mutation is normalized; every current
    # financial, line, CNY and timestamp operand must still be byte-identical.
    material = {'header': before, **{k: reader.rows(sql, (shipment,)) for k, sql in COST_QUERIES.items()}}
    require(targeted._fingerprint(material) == publication['source_revision'], 'supplier_cost_inputs_changed')
    nm_ids = json.loads(publication['affected_nm_ids_json'])
    require(type(nm_ids) is list and nm_ids and len(nm_ids) == len(set(nm_ids))
            and all(type(nm) is int and nm > 0 for nm in nm_ids), 'replay_sku_scope_invalid')
    marks = ','.join('?' for _ in nm_ids)
    actual = reader.rows(f'''SELECT * FROM {P}warehouse_functional_balances WHERE version_id=?
        AND warehouse_key IN ('production','china_to_ff') AND nm_id IN ({marks}) ORDER BY warehouse_key,nm_id''',
                        (version_id, *nm_ids))
    actual = [{**{k: v for k, v in r.items() if k not in {'version_id', 'provenance_json'}},
               'provenance': json.loads(r['provenance_json'])} for r in actual]
    require(targeted._fingerprint(actual) == publication['target_after_digest'], 'replay_native_rows_changed')
    # Recompute the native publisher's foreign-row digest, rather than merely
    # trusting two equal stored metadata fields. This excludes affected nmIDs
    # exactly as the original targeted publication contract does.
    non_target = reader.rows(f'''SELECT warehouse_key,nm_id,quantity,wac_rub,capital_rub,
        cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,
        wb_in_way_from_client,provenance_json FROM {P}warehouse_functional_balances
        WHERE version_id=? AND nm_id NOT IN ({marks}) ORDER BY warehouse_key,nm_id''', (version_id, *nm_ids))
    require(targeted._fingerprint(non_target) == publication['non_target_after_digest'], 'replay_native_foreign_rows_changed')
    state = reader.one(f'SELECT * FROM {P}warehouse_supplier_cost_states WHERE version_id=? AND shipment_id=?',
                       (version_id, shipment))
    records = _sources(rows_after, shipment, targeted_policy=True)
    require(records and not after.get('actual_ff_acceptance_date')
            and after.get('actual_shipment_date') == day, 'replay_date_or_source_missing')
    for item in records:
        source = item['source']
        require(item['stage'] == 'china_to_ff' and item['nm_id'] in nm_ids
                and source.get('business_date') == source.get('actual_shipment_date') == day
                and source.get('source_fingerprint') == state['source_fingerprint']
                and source.get('calculation_fingerprint') == state['calculation_fingerprint']
                and not source.get('cost_blockers') and _number(source['flow_quantity']) > 0
                and _number(source['flow_capital_rub']) > 0, 'replay_cost_state_changed')
    require(len({(r['stage'], r['nm_id']) for r in records}) == len(records), 'duplicate_replay_source')
    return dict(version_id=version_id, shipment_id=shipment, business_date=day,
                publication=publication, undo_digest=undo_row['manifest_digest'], sources=records,
                supplier_cost_state=state, header_after=after)


def capture_foreign_suffix_sources(conn, *, saved_by_day, documents, targeted_version_ids, date_from, date_to):
    require(conn.in_transaction and conn.row_factory is sqlite3.Row
            and conn.execute('PRAGMA query_only').fetchone()[0] == 1, 'query_only_capture_required')
    require(saved_by_day and sorted(saved_by_day)[0] == date_from and sorted(saved_by_day)[-1] == date_to,
            'date_scope_changed')
    require(0 <= (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days <= 31, 'date_scope_budget')
    reader = _Reader(conn)
    # The action owns and fences completeness of its pre-native selector,
    # including late corrections whose business boundary predates this suffix.
    require(len(targeted_version_ids) <= 32 and len(targeted_version_ids) == len(set(targeted_version_ids)),
            'targeted_selection_changed')
    docs = _document_set(documents)
    oldest = min(_timestamp(json.loads(saved['version']['source_watermarks_json'])['captured_at'])
                 for saved in saved_by_day.values())
    # These may include the 583 operations just simulated by our owner. Their
    # original prestate and exact T1 plans are fenced by the action; including
    # them in this *foreign pre-money* fence would demand future rows too soon.
    candidates = [dict(r) for r in conn.execute(f'''SELECT * FROM {P}warehouse_business_operations
        WHERE (julianday(posted_at)>=julianday(?) OR julianday(posted_at) IS NULL)
        AND business_date<=? ORDER BY business_date,posted_at,operation_id''',
        (oldest.isoformat(), date_to)).fetchmany(100001)]
    require(len(candidates) <= 100000 and len(canonical(candidates).encode()) <= MAX_BYTES, 'late_operations_budget')
    # SQLite julianday rounds below milliseconds. Inclusive SQL is only a
    # conservative superset; Python's aware microsecond comparison owns >.
    late = [row for row in candidates if _timestamp(row['posted_at']) > oldest]
    require({r['operation_id'] for r in late} <= {d['document_id'] for d in docs}, 'unknown_late_native_operation')
    with localcontext() as context:
        context.prec = 160
        replays = [_capture_replay(reader, identity) for identity in targeted_version_ids]
        require(all(r['business_date'] <= date_to for r in replays), 'replay_after_scope')
        replays.sort(key=lambda r: (r['business_date'], r['version_id']))
    require(len({r['shipment_id'] for r in replays}) == len(replays), 'multiple_replays_for_shipment')
    result = dict(contract=CONTRACT, date_from=date_from, date_to=date_to, replays=replays,
                  saved_digests={d: fingerprint(s) for d, s in sorted(saved_by_day.items())},
                  documents_digest=fingerprint(docs), late_native_operations=late,
                  query_fence=json.loads(canonical(ready.pin_queries(conn, reader.queries))))
    require(len(canonical(result).encode()) <= MAX_BYTES, 'capture_budget')
    result['source_digest'] = fingerprint(result)
    return result


def _replace_supplier(balances, replay):
    shipment = replay['shipment_id']
    # Require the old identity to be isolatable; never insert an unrelated flow.
    require(_sources(balances, shipment), 'supplier_source_identity_missing')
    replacements = {(r['stage'], r['nm_id']): r['source'] for r in replay['sources']}
    rows = {(r['warehouse_key'], int(r['nm_id'])): deepcopy(r) for r in balances}
    keys = {k for k in rows if k[0] in SUPPLIER_STAGES} | set(replacements)
    for key in sorted(keys):
        original = rows.get(key)
        sources = deepcopy(original['provenance']['source_records']) if original else []
        kept = [s for s in sources if s.get('shipment_id') != shipment]
        if key in replacements: kept.append(deepcopy(replacements[key]))
        if kept == sources: continue
        if not kept:
            rows.pop(key, None); continue
        prototype = original or dict(warehouse_key=key[0], nm_id=key[1], wb_quantity='0',
                                    wb_in_way_to_client='0', wb_in_way_from_client='0', provenance={})
        rows[key] = _supplier_row(prototype, kept)
    return [rows[k] for k in sorted(rows)]


def _supplier_row(prototype, sources):
    q = sum((_number(s['flow_quantity']) for s in sources), Decimal(0))
    c = sum((_number(s['flow_capital_rub']) for s in sources), Decimal(0))
    require(q > 0, 'supplier_replacement_empty')
    covered = sum((_number(s['flow_quantity']) for s in sources
        if s.get('cost_freshness') != 'unavailable' and _number(s['flow_capital_rub']) > 0), Decimal(0))
    complete = covered == q and c > 0
    qualities = sorted({s['quality'] for s in sources})
    return {**deepcopy(prototype), 'quantity': format(q, 'f'), 'capital_rub': format(c, 'f'),
            'wac_rub': format(c / q, 'f') if complete else None, 'cost_covered_quantity': format(covered, 'f'),
            'quality': qualities[0] if len(qualities) == 1 else 'mixed:' + ','.join(qualities),
            'certified': int(complete and all(s.get('expenses_complete_certification') for s in sources)),
            'provenance': {**deepcopy(prototype['provenance']), 'source_records': deepcopy(sources)}}


def rebuild_foreign_suffix_day(saved, *, day, documents, foreign_capture):
    require(foreign_capture.get('contract') == CONTRACT and foreign_capture['source_digest'] ==
            fingerprint({k: v for k, v in foreign_capture.items() if k != 'source_digest'}), 'capture_changed')
    require(foreign_capture['saved_digests'].get(day) == fingerprint(saved), 'saved_source_changed')
    docs = _document_set(documents)
    require(fingerprint(docs) == foreign_capture['documents_digest'], 'native_documents_changed')
    with localcontext() as context:
        context.prec = 160
        balances, supplier_proof = stages._supplier_revision(saved['balances'], day=day,
            receipts=[d for d in docs if d.get('kind') == 'china_acceptance'])
        original = {(r['warehouse_key'], int(r['nm_id'])): r for r in saved['balances']}
        # The legacy receipt helper recalculates every supplier row. Preserve
        # untouched foreign metadata and never promote partial cost coverage.
        restored = []
        for row in balances:
            prior = original[(row['warehouse_key'], int(row['nm_id']))]
            if row['warehouse_key'] not in SUPPLIER_STAGES or row['provenance']['source_records'] == prior['provenance']['source_records']:
                restored.append(deepcopy(prior))
            else:
                restored.append(_supplier_row(prior, row['provenance']['source_records']))
        balances = restored
        physical, physical_proof = stages._physical_ff_revision(saved, day=day, documents=docs)
        balances = [r for r in balances if r['warehouse_key'] != 'ff'] + physical
        companions = deepcopy(saved['companions']); applied = []
        for replay in foreign_capture['replays']:
            if replay['business_date'] > day: continue
            before = _sources(balances, replay['shipment_id'])
            frontier = json.loads(saved['version']['source_watermarks_json'])['captured_at']
            later = _timestamp(frontier) >= _timestamp(replay['publication']['completed_at'])
            if later:
                # Targeted replay materializes at six decimal places. A later
                # genuine hourly edition retains longer native arithmetic;
                # prove compatibility without rounding or replacing its money.
                expected = {(r['stage'], r['nm_id']): r['source'] for r in replay['sources']}
                actual = {(r['stage'], r['nm_id']): r['source'] for r in before}
                require(len(actual) == len(before) and set(actual) == set(expected), 'later_supplier_scope_changed')
                for key, source in actual.items():
                    wanted = expected[key]
                    require(source.get('actual_shipment_date') == replay['business_date']
                            and _number(source['flow_quantity']) == _number(wanted['flow_quantity'])
                            and _number(source['flow_capital_rub']).quantize(Decimal('.000001')) ==
                            _number(wanted['flow_capital_rub']).quantize(Decimal('.000001')),
                            'later_supplier_cost_changed')
                applied.append(dict(version_id=replay['version_id'], shipment_id=replay['shipment_id'],
                                    business_date=replay['business_date'], already_in_native_frontier=True))
                continue
            if before != replay['sources']:
                balances = _replace_supplier(balances, replay)
                applied.append(dict(version_id=replay['version_id'], shipment_id=replay['shipment_id'],
                                    business_date=replay['business_date'], before=before, after=deepcopy(replay['sources'])))
            states = companions['supplier_cost_states']
            companions['supplier_cost_states'] = [r for r in states if r['shipment_id'] != replay['shipment_id']] + [deepcopy(replay['supplier_cost_state'])]
        balances.sort(key=lambda r: (r['warehouse_key'], int(r['nm_id'])))
    require([r for r in balances if r['warehouse_key'] not in SUPPLIER_STAGES | {'ff'}] ==
            [r for r in saved['balances'] if r['warehouse_key'] not in SUPPLIER_STAGES | {'ff'}], 'foreign_non_supplier_changed')
    return dict(balances=balances, companions=companions,
                proof=dict(contract=CONTRACT, capture_digest=foreign_capture['source_digest'],
                           supplier_receipts=supplier_proof, physical_ff=physical_proof, targeted_rebases=applied))
