"""Recover yesterday from a real archive inside an open no-document gap.

This is part of ordinary book preparation, under its pinned read transaction and
publication CAS. It never reopens a closed day or reconstructs absent quantities.
"""
from copy import deepcopy
from datetime import date, timedelta
import logging

from packages.application.fbs_snapshot_cost import (
    evaluate_candidate, close_candidate_period, fingerprint,
)
from packages.application.fbs_accounting_historical_sources import archived_quantity_snapshot
from packages.application.shared_sku_cost_sources import capture_wb_component
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_inventory_presentation import capture_retained_stages, FbsInventorySnapshot


def recover_previous_day(book, *, capture, db, conn):
    try:
        return _recover_previous_day(book, capture=capture, db=db, conn=conn)
    except ValueError as exc:
        # Current capture/document/proof admission already passed in the caller.
        # An incompatible historical observation must not invalidate that day.
        code = str(exc).split(':', 1)[0]
        if code.startswith('historical_archived_quantity_') or code in {
            'cost_continuity_mapping_changed', 'cost_continuity_scope_or_documents_incomplete',
            'retained_stage_version_or_digest_mismatch', 'retained_stage_catalog_mismatch',
            'retained_wb_shared_cost_mismatch',
        }:
            logging.getLogger(__name__).warning('Archived previous-day recovery skipped: %s', code)
            return book
        raise


def _recover_previous_day(book, *, capture, db, conn):
    """Add only the preceding missing date; preserve today's physical valuation.

    The caller has already evaluated today's complete capture, validating the
    existing continuity proof and all observed documents. Only its open period
    can be rebuilt with a newer opening. Earlier closed periods stay immutable.
    """
    today = capture['business_date']
    day = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    state = book['state']
    current = state['periods'].get(today, {})
    proof = current.get('cost_continuity', {})
    if (day in state['periods'] or day not in proof.get('missing_dates', [])
            or current.get('status') != 'open' or current.get('quality') != 'preliminary'
            or state.get('pending_documents')
            or [d for d, p in state['periods'].items() if p['status'] == 'open'] != [today]):
        return book
    # An older installation without an archive cannot supply historical facts.
    from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (REGISTRY_RUNS_TABLE,)).fetchone() is None:
        return book
    try:
        snapshot = archived_quantity_snapshot(conn, day=day, observed_at=capture['captured_at'])
    except ValueError as exc:
        if str(exc) == 'historical_archived_quantity_missing:' + day:
            return book
        raise
    ids = sorted({row['nm_id'] for row in snapshot['rows']})
    wb = capture_wb_component(db, day=day, nm_ids=ids, connection=conn)
    if not wb.get('complete') or wb.get('authority_complete') is not True:
        return book
    retained = capture_retained_stages(db, day=day, wb_version_id=wb['version_id'],
                                       nm_ids=ids, connection=conn)
    candidate = deepcopy(book)
    state = candidate['state']
    del state['periods'][today]
    dated = deepcopy(capture)
    dated['business_date'] = day
    dated['quantity_snapshot'] = snapshot
    dated['source_digest'] = fingerprint({'archived_quantity_snapshot': snapshot,
                                        'document_capture_source_digest': capture['source_digest']})
    state = evaluate_candidate(state, dated, allow_cost_continuity_gap=True)
    from packages.application.operator_ff_overhead import assert_day_can_close
    assert_day_can_close(conn, day)
    state = close_candidate_period(state, day, today=today)
    candidate['wb_days'][day] = wb
    candidate['retained_days'][day] = retained
    candidate['shared_days'][day] = build_shared_cost_day(state, wb, day)
    candidate['presentations'][day] = FbsInventorySnapshot(
        fbs_state=state, wb_capture=wb, retained=retained, day=day).payload()
    state = evaluate_candidate(state, capture, allow_cost_continuity_gap=True)
    # With the proven absence of intervening documents, changing the opening
    # quantity cannot change today's quantity, WAC or capital. Fail on drift.
    def values(period):
        return {key: tuple(row.get(field) for field in ('quantity', 'wac_rub', 'capital_rub'))
                for key, row in period['rows'].items()}
    if values(current) != values(state['periods'][today]):
        raise ValueError('archived_gap_current_valuation_changed')
    candidate['state'] = state
    return candidate
