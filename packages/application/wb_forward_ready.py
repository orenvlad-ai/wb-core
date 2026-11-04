"""Explicit offline admission for forward WB valuation Ready publication.

The default Ready writer is unchanged. This admission binds an already posted
forward event to its native supplier operands and saved historical Ready cells.
No publisher, clock override, source bypass, or production endpoint lives here.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, timedelta
import json

from packages.application import ready_publication as ready
from packages.application import wb_forward_valuation as forward

CONTRACT = 'wb_forward_ready_v1'
DATED_METADATA = ('fbs_accounting_bindings', 'fbs_accounting_targets', 'daily_trading_pool')
SUPPLIER_QUERIES = (
    ('supplier_shipments', 'shipment_id', 'shipment_id'),
    ('supplier_shipment_lines', 'shipment_id', 'shipment_id,sort_order,line_id'),
    ('cny_ledger_operations', 'source_order_id', 'sequence_key,operation_id'),
    ('cny_documents', 'source_order_id', 'operation_date,operation_datetime,document_id'),
    ('supplier_financial_documents', 'supplier_order_id', 'document_date,document_id'),
    ('supplier_financial_expense_lines', 'supplier_order_id', 'supplier_order_id,financial_document_id,sort_order,line_id'),
    ('supplier_bank_operation_assignments', 'supplier_order_id', 'supplier_order_id,semantic_operation_id'),
    ('supplier_payment_fee_confirmations', 'supplier_order_id', 'supplier_order_id,payment_document_id,confirmation_id'),
)


def _fail(reason):
    raise ready.ReadyPublicationConflict('forward_ready_' + reason)


def _data(plan):
    return next(s for s in plan.sheets if s.sheet_name == 'DATA_VITRINA')


def _rows(sheet):
    rows = {str(row[1]): row for row in sheet.rows}
    if len(rows) != len(sheet.rows):
        _fail('duplicate_row_identity')
    return rows


def prepare_forward_ready_admission(conn, *, request, plan, expected, publication_date):
    """Read-only complete proof, to be independently rebuilt by the Ready owner."""
    from packages.application import warehouse_functional as wf
    from packages.application.ready_rollover import headers
    from packages.application.web_vitrina_window_v3 import _select_bindings
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    from packages.business_time import default_business_as_of_date
    from datetime import datetime, timezone

    from packages.application.sheet_vitrina_v1 import (
        _require_date_columns_or_infer, _require_temporal_slots_or_default,
    )
    dates = _require_date_columns_or_infer({'date_columns': plan.date_columns}, _data(plan).header)
    if len(dates) != len(set(dates)):
        _fail('duplicate_date_columns')
    _require_temporal_slots_or_default(
        delivery_bundle={'temporal_slots': [asdict(slot) for slot in plan.temporal_slots]},
        as_of_date=plan.as_of_date, date_columns=dates)

    cutoff = request['day']
    requested_ids = set(request['nm_ids'])
    if len(request['nm_ids']) != len(requested_ids):
        _fail('duplicate_request_scope')
    if (date.fromisoformat(cutoff).isoformat() != cutoff or publication_date < cutoff
            or publication_date not in plan.date_columns
            or (plan.as_of_date < cutoff and (plan.as_of_date != (date.fromisoformat(publication_date)-timedelta(days=1)).isoformat()
                or plan.as_of_date not in plan.date_columns))
            or expected.as_of_date != plan.as_of_date):
        _fail('publication_boundary')
    prior = (date.fromisoformat(cutoff) - timedelta(days=1)).isoformat()
    raw_events = [dict(r) for r in conn.execute(
        'SELECT * FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type=? AND source_id=? ORDER BY nm_id',
        (forward.EVENT, request['request_id']))]
    if (not raw_events or len(raw_events) != len(requested_ids)
            or {r['business_date'] for r in raw_events} != {cutoff}
            or {r['nm_id'] for r in raw_events} != requested_ids):
        _fail('event_scope')
    versions = {r['version_id'] for r in raw_events}
    if len(versions) != 1:
        _fail('event_version')
    version = next(iter(versions))
    anchors = {int(r['nm_id']): wf._initial_valuation_anchor(json.loads(r['provenance_json']))
        for r in conn.execute("SELECT nm_id,provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND warehouse_key='wb'", (version,))
        if int(r['nm_id']) in set(request['nm_ids'])}
    daily = [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE as_of_date=?', (prior,))]
    accepted = conn.execute(f"SELECT bundle_version,as_of_date,plan_json FROM {ready.TABLE} "
        "WHERE bundle_version=? AND json_extract(plan_json,'$.metadata.wb_forward_ready.contract')=? "
        "AND json_extract(plan_json,'$.metadata.wb_forward_ready.request.request_id')=? "
        "ORDER BY as_of_date DESC LIMIT 1", (expected.bundle_version, CONTRACT, request['request_id'])).fetchone()
    for actual in raw_events:
        actual['provenance'] = json.loads(actual.pop('provenance_json'))
    if accepted is None:
        wanted = forward.prepare_events(conn, request_id=request['request_id'], day=cutoff,
            shipment_id=request['shipment_id'], receipt_id=request['receipt_id'], daily_rows=daily, anchors=anchors)
    else:
        # Supplier operands may legitimately change after this dated admission.
        # The accepted Ready seals the event/source proof, not future allocation.
        previous = json.loads(accepted['plan_json'])['metadata']['wb_forward_ready']
        opening = {int(r['nm_id']): r for r in daily}
        wanted = []
        for actual in raw_events:
            nm = actual['nm_id']
            proof = actual['provenance'].get('forward_valuation', {})
            evidence = proof.get('source_evidence', {})
            if evidence.get('shipment_id') != request['shipment_id'] or evidence.get('receipt_id') != request['receipt_id']:
                _fail('accepted_source_identity')
            expected_event = forward.make_event(request_id=request['request_id'], day=cutoff,
                nm_id=nm, opening_quantity=opening[nm]['quantity'], opening_wac=opening[nm]['wac_rub'],
                old_supplier_unit=proof['old_supplier_unit_rub'], new_supplier_unit=proof['new_supplier_unit_rub'],
                source_evidence=evidence, anchor=anchors[nm])
            wanted.append(expected_event)
        if previous.get('request') != request or previous.get('events_digest') != forward.digest(wanted):
            _fail('accepted_event_changed')
    if len(wanted) != len(raw_events) or len(wanted) != len(requested_ids):
        _fail('event_scope')
    for actual, candidate in zip(raw_events, wanted):
        forward.check_identity(actual, candidate)
    active = conn.execute('SELECT v.version_id,v.business_effective_date FROM sheet_vitrina_v1_warehouse_functional_active a JOIN sheet_vitrina_v1_warehouse_functional_versions v ON v.version_id=a.version_id WHERE a.slot=1').fetchone()
    if not active or active['business_effective_date'] != publication_date:
        _fail('active_date')
    queries = [(f'SELECT * FROM sheet_vitrina_v1_{table} WHERE {field}=? ORDER BY {order}', (request['shipment_id'],))
               for table, field, order in SUPPLIER_QUERIES]
    if accepted is not None:
        queries = []
    # Other event/receipt/daily/active operands are covered by native material revisions.
    protected_dates = sorted(day for day in plan.date_columns if day < cutoff)
    default_date = default_business_as_of_date(datetime.fromisoformat(publication_date).replace(hour=12, tzinfo=timezone.utc))
    bindings, _ = _select_bindings(conn, headers(conn), protected_dates, expected.bundle_version, default_date)
    sources, frozen, parsed = {}, {}, {}
    if accepted is not None:
        key = (accepted['bundle_version'], accepted['as_of_date'])
        sources[key] = ready.capture_expected(conn, bundle_version=key[0], as_of_date=key[1], authority=expected.authority)
    for binding in bindings:
        if binding.source_key is None:
            _fail('saved_date_missing:' + binding.date)
        key = tuple(binding.source_key)
        if key not in sources:
            donor = ready.capture_expected(conn, bundle_version=key[0], as_of_date=key[1], authority=expected.authority)
            sources[key] = donor
        if key not in parsed:
            parsed[key] = _deserialize_sheet_vitrina_plan(sources[key].plan_json)
        saved = parsed[key]
        sheet = _data(saved)
        index = sheet.header.index(binding.date)
        cells = saved.metadata.get('server_cell_presentation', {})
        frozen[binding.date] = {
            'source': list(key),
            'cells': {key: {'value': deepcopy(row[index]), 'presentation': deepcopy(cells.get(key, {}).get(binding.date))}
                      for key, row in _rows(sheet).items()},
            'metadata': {name: deepcopy(saved.metadata.get(name, {}).get(binding.date)) for name in DATED_METADATA},
        }
        # A copied binding must still identify the Ready that originally owns it.
        binding_value = frozen[binding.date]['metadata']['fbs_accounting_bindings']
        if binding_value and binding_value.get('ready_target'):
            frozen[binding.date]['metadata']['fbs_accounting_targets'] = deepcopy(binding_value['ready_target'])
    payload = dict(contract=CONTRACT, request=deepcopy(request), cutoff=cutoff,
        publication_date=publication_date, target_fingerprint=expected.fingerprint,
        candidate_dates=list(plan.date_columns), active_version=active['version_id'],
        events_digest=forward.digest(wanted), source_pins=ready.pin_queries(conn, queries),
        ready_sources=[dict(bundle_version=k[0], as_of_date=k[1], plan_digest=ready.digest(v.plan_json)) for k,v in sorted(sources.items())],
        frozen=frozen)
    return {**payload, 'manifest_digest': forward.digest(payload)}


def validate_admission(conn, *, admission, plan, expected, publication_date):
    rebuilt = prepare_forward_ready_admission(conn, request=admission['request'], plan=plan,
        expected=expected, publication_date=publication_date)
    if rebuilt != admission:
        _fail('proof_changed')


def check_admission(conn, admission):
    """Only bounded pinned reads under the existing writer locks; no rebuild."""
    ready.check_queries(conn, admission['source_pins'])
    for source in admission['ready_sources']:
        current = ready.capture_expected(conn, bundle_version=source['bundle_version'], as_of_date=source['as_of_date'])
        if current.plan_json is None or ready.digest(current.plan_json) != source['plan_digest']:
            _fail('saved_ready_changed')


def forward_view(plan, cutoff):
    """Native economics and inventory preparation cannot visit excluded dates."""
    days = [day for day in plan.date_columns if day >= cutoff]
    metadata = deepcopy(plan.metadata)
    for by_date in metadata.get('server_cell_presentation', {}).values():
        for day in list(by_date):
            if day < cutoff:
                del by_date[day]
    for name in DATED_METADATA:
        if name in metadata:
            metadata[name] = {day: value for day, value in metadata[name].items() if day >= cutoff}
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name == 'DATA_VITRINA':
            indices = [i for i, name in enumerate(sheet.header) if name not in plan.date_columns or name in days]
            sheet = replace(sheet, header=[sheet.header[i] for i in indices],
                rows=[[row[i] for i in indices] for row in sheet.rows], column_count=len(indices))
        sheets.append(sheet)
    return replace(plan, date_columns=days, temporal_slots=[s for s in plan.temporal_slots if s.column_date >= cutoff],
                   metadata=metadata, sheets=sheets)


def restore_frozen(plan, *, original, admission):
    """Retain saved values/quality by semantic row identity; never by position."""
    metadata = deepcopy(plan.metadata)
    cells = metadata.setdefault('server_cell_presentation', {})
    rows = _rows(_data(plan))
    forward_header = _data(plan).header
    header = _data(original).header
    restored = []
    for key, row in rows.items():
        result = []
        for column in header:
            if column in admission['frozen']:
                saved = admission['frozen'][column]['cells'].get(key)
                if saved is None:
                    # New rows have no historical value; no zero or invented quality.
                    result.append('')
                    cells.setdefault(key, {}).pop(column, None)
                else:
                    result.append(deepcopy(saved['value']))
                    if saved['presentation'] is None:
                        cells.setdefault(key, {}).pop(column, None)
                    else:
                        cells.setdefault(key, {})[column] = deepcopy(saved['presentation'])
            else:
                result.append(row[forward_header.index(column)])
        restored.append(result)
    original_keys = set(_rows(_data(original)))
    if original_keys - rows.keys():
        _fail('candidate_rows_lost')
    for day, saved in admission['frozen'].items():
        if set(saved['cells']) - rows.keys():
            _fail('saved_rows_lost:' + day)
        for name, value in saved['metadata'].items():
            if value is not None:
                metadata.setdefault(name, {})[day] = deepcopy(value)
    metadata['wb_forward_ready'] = {key: deepcopy(value) for key,value in admission.items() if key not in ('frozen', 'source_pins')}
    metadata['wb_forward_ready']['frozen_cells_digest'] = forward.digest(admission['frozen'])
    sheets = [replace(sheet, header=list(header), rows=restored, row_count=len(restored), column_count=len(header))
              if sheet.sheet_name == 'DATA_VITRINA' else sheet for sheet in plan.sheets]
    return replace(plan, date_columns=original.date_columns, temporal_slots=original.temporal_slots, metadata=metadata, sheets=sheets)
