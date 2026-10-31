"""Scoped fail-closed ready publication after a proven accounting quality failure.

This grants no book-writing authority and cannot be enabled by persisted plan
metadata. Only the live heavy owner can carry the exact failed book revision.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

_REASON = 'incomplete_period_cannot_close'
_SCOPE = ContextVar('fbs_accounting_degraded_scope', default=None)
METADATA_KEY = 'fbs_accounting_unavailable'


def _root(runtime):
    return Path(getattr(runtime, 'runtime_dir', runtime)).resolve()


def _validate(root, versions, book, version):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.ready_publication import ReadyPublicationConflict
    owner = require_heavy_owner(root)
    if owner.operation != "cycle":
        raise ReadyPublicationConflict("degraded_accounting_cycle_owner_required")
    error = (book or {}).get('publication_error') or {}
    if (versions.get('fbs_accounting_status') != 'unavailable'
            or versions.get('fbs_accounting_reason') != _REASON
            or not versions.get('fbs_book') or versions['fbs_book'] != version
            or not book or not book.get('active') or error.get('reason') != _REASON):
        raise ReadyPublicationConflict('degraded_accounting_proof_changed')
    periods = book.get('state', {}).get('periods', {})
    open_days = [day for day, value in periods.items() if value.get('status') == 'open']
    if not open_days:
        raise ReadyPublicationConflict('degraded_accounting_open_period_missing')
    if versions.get('fbs_accounting_blocked_date', min(open_days)) != min(open_days):
        raise ReadyPublicationConflict('degraded_accounting_blocked_date_changed')
    return {'runtime_dir': str(root), 'book_version': version, 'reason': _REASON,
            'invalid_from_date': min(open_days),
            'closed_dates': sorted(day for day, value in periods.items() if value.get('status') == 'closed')}


@contextmanager
def degraded_accounting_scope(runtime, versions):
    """Carry a verified unavailable warehouse proof through one owned stage."""
    if versions.get('fbs_accounting_status') != 'unavailable':
        yield
        return
    from packages.application.fbs_accounting_runtime import load
    root = _root(runtime)
    book, version = load(root)
    proof = _validate(root, versions, book, version)
    token = _SCOPE.set((root, dict(versions), proof))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_degraded_accounting(runtime, *, book=None, book_version=None):
    """Revalidate live ownership and revision; a scope never follows new state."""
    scope = _SCOPE.get()
    if scope is None:
        return None
    from packages.application.ready_publication import ReadyPublicationConflict
    root, versions, expected = scope
    if _root(runtime) != root:
        raise ReadyPublicationConflict('degraded_accounting_runtime_changed')
    if book is None:
        from packages.application.fbs_accounting_runtime import load
        book, book_version = load(root)
    proof = _validate(root, versions, book, book_version)
    if proof != expected:
        raise ReadyPublicationConflict('degraded_accounting_proof_changed')
    return proof


def _affected(proof, day):
    return bool(day and day >= proof['invalid_from_date'] and day not in proof['closed_dates'])


def _dependent_metric(metric):
    from packages.application.web_vitrina_management_history import TARGET_METRICS
    return (metric in TARGET_METRICS
            or metric in {'stock_total', 'total_stock_total'}
            or metric.startswith(('own_', 'total_own_', 'avg_own_', 'inventory_', 'total_inventory_')))


def materialize_degraded_accounting(plan, *, proof):
    """Blank uncertain operands and dependent estimates, preserving source facts."""
    if proof is None:
        return plan
    from packages.application.fbs_inventory_presentation import SOURCE
    days = [day for day in plan.date_columns if _affected(proof, day)]
    if not days:
        return plan
    metadata = deepcopy(dict(plan.metadata or {}))
    metadata[METADATA_KEY] = {key: value for key, value in proof.items() if key != 'runtime_dir'}
    metadata[METADATA_KEY].update(dates=days, quality='unavailable')
    cells = metadata.setdefault('server_cell_presentation', {})
    for key in ('fbs_accounting_bindings', 'fbs_accounting_targets'):
        mapping = metadata.get(key, {})
        for day in days:
            mapping.pop(day, None)
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name == 'STATUS':
            from packages.application.sheet_vitrina_v1_live_plan import STATUS_HEADER
            rows = [list(row) + [''] * max(0, 11-len(row)) for row in sheet.rows
                    if not row or str(row[0]) != 'fbs_snapshot_accounting']
            rows.append(['fbs_snapshot_accounting', 'not_available', '', '', '',
                min(days), max(days), '', '', '',
                'Себестоимость FBS не подтверждена; учётный день не закрыт. Зависимые оценки недоступны.'])
            sheets.append(replace(sheet, header=STATUS_HEADER, rows=rows, row_count=len(rows),
                column_count=11, write_rect=f'A1:K{len(rows)+1}'))
            continue
        if sheet.sheet_name != 'DATA_VITRINA':
            sheets.append(sheet)
            continue
        rows = deepcopy(sheet.rows)
        identities = {str(row[1]) for row in rows if len(row) > 1}
        identities.update(cells)
        affected = {identity for identity in identities if _dependent_metric(identity.split('|')[-1])}
        for identity in affected:
            for day in days:
                from packages.application.web_vitrina_official_fbs import is_frozen_official_inventory_quantity
                if is_frozen_official_inventory_quantity(identity.split("|")[-1], day, cells.get(identity, {}).get(day, {})):
                    continue
                cells.setdefault(identity, {})[day] = {
                    'source': SOURCE, 'source_as_of_date': day,
                    'state': 'unavailable', 'quality_state': 'unavailable', 'completeness_state': 'unavailable',
                    'management_value': '', 'candidate_only': False, 'tone': 'warning',
                    'reason_code': _REASON, 'book_version': proof['book_version'],
                    'reason': 'Себестоимость FBS не подтверждена. Учётный день остаётся незакрытым; зависимая оценка недоступна.'}
        for row in rows:
            if len(row) > 1 and str(row[1]) in affected:
                for day in days:
                    if not is_frozen_official_inventory_quantity(str(row[1]).split('|')[-1], day, cells.get(str(row[1]), {}).get(day, {})):
                        row[sheet.header.index(day)] = ''
        sheets.append(replace(sheet, rows=rows))
    return replace(plan, sheets=sheets, metadata=metadata)


def degraded_inventory_date(plan, day):
    """Persisted quality may suppress writes, but never grant publication authority."""
    quality = (plan.metadata or {}).get(METADATA_KEY) or {}
    return bool(day and quality.get('reason') == _REASON and day in quality.get('dates', []))


def reconcile_recovered_accounting(plan, *, book):
    """Release suppression only for dates now backed by a real good book view."""
    quality = (plan.metadata or {}).get(METADATA_KEY)
    if not quality or not book or not book.get('active') or book.get('publication_error'):
        return plan
    recovered = {day for day, presentation in book.get('presentations', {}).items()
                 if presentation.get('quality') not in {'incomplete', 'unavailable'}}
    remaining = [day for day in quality.get('dates', []) if day not in recovered]
    if remaining == quality.get('dates', []):
        return plan
    metadata = deepcopy(plan.metadata)
    # Clear suppression evidence before the ordinary materializer/recalculation.
    # Previously blank DATA values stay blank until a real producer fills them.
    for by_date in metadata.get('server_cell_presentation', {}).values():
        for day in recovered:
            if (by_date.get(day) or {}).get('reason_code') == _REASON:
                by_date.pop(day, None)
    if remaining:
        metadata[METADATA_KEY]['dates'] = remaining
    else:
        metadata.pop(METADATA_KEY, None)
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name == 'STATUS' and not remaining:
            rows = [row for row in sheet.rows if not row or str(row[0]) != 'fbs_snapshot_accounting']
            sheet = replace(sheet, rows=rows, row_count=len(rows), write_rect=f'A1:K{len(rows)+1}')
        sheets.append(sheet)
    return replace(plan, metadata=metadata, sheets=sheets)


def apply_degraded_rows(rows, *, presentation):
    """Last read overlay, including History workers without the writer context.

    Persisted evidence can only withhold operands. It cannot authorize a write
    or select a different book. Apply after group and historical restorations.
    """
    dates = {}
    for by_date in presentation.values():
        for day, cell in by_date.items():
            if cell.get('reason_code') == _REASON and cell.get('state') == 'unavailable':
                dates[day] = cell
    if not dates:
        return rows
    result = []
    for row in rows:
        if not _dependent_metric(row.metric_key):
            result.append(row)
            continue
        from packages.application.web_vitrina_official_fbs import is_frozen_official_inventory_quantity
        affected = {day: deepcopy(cell) for day, cell in dates.items() if day in row.values_by_date
            and not is_frozen_official_inventory_quantity(row.metric_key, day,
                presentation.get(row.row_id, {}).get(day, {}))}
        result.append(replace(row,
            values_by_date={**row.values_by_date, **{day: '' for day in affected}},
            presentation_by_date={**row.presentation_by_date, **affected}))
    return result
