"""Read-only FBS demand from certified listing ranges and observed order stages.

Receipts certify a listing only after its pagination chain starts at zero and
finishes. A successful hourly run alone does not certify a calendar day. No
stock or accounting state is derived from this analytics store.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from statistics import median
import sqlite3
from typing import Any, Iterable, Mapping

from packages.business_time import CANONICAL_BUSINESS_TIMEZONE, business_date_from_timestamp
from packages.application.wb_fbs_orders import (
    OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE, STATUS_OBSERVATIONS_TABLE,
    KNOWN_SUPPLIER_STATUSES, KNOWN_WB_STATUSES,
)


@dataclass(frozen=True)
class FbsDemandHistory:
    samples_by_facility: dict[str, dict[int, dict[str, float | None]]]
    complete_dates: tuple[str, ...]
    excluded_dates: tuple[str, ...]
    coverage: dict[str, Any]

    def samples(self, facility_id: str, nm_id: int) -> dict[str, float | None]:
        return self.samples_by_facility.get(facility_id, {}).get(nm_id, {})


@dataclass(frozen=True)
class FbsDemandEstimate:
    daily_demand: float | None
    used_dates: tuple[str, ...]
    excluded_dates: tuple[str, ...]
    incomplete_dates: tuple[str, ...]
    baseline_daily_sales: float
    valid_day_threshold: float
    warning: str
    notes: tuple[str, ...]


def _certified_ranges(receipts: Iterable[Mapping[str, Any]]) -> list[tuple[int, int]]:
    chains: dict[tuple[int, int], tuple[int, bool]] = {}
    ranges = []
    for run in receipts:
        try:
            start, end = int(run['window_date_from']), int(run['window_date_to'])
            cursor, next_cursor = int(run['start_cursor']), int(run['next_cursor'])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= 0 or end < start or cursor < 0 or next_cursor < 0:
            continue
        key = start, end
        previous = chains.get(key)
        connected = cursor == 0 or (previous is not None and previous[0] == cursor and previous[1])
        # Drift counts concern status vocabulary, even outside the listing window.
        # Reject unknown status only on its SKU/day, not the whole listing.
        clean = connected
        chains[key] = next_cursor, clean
        if clean and run.get('listing_complete') is True:
            ranges.append(key)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return merged


def _complete_days(ranges: list[tuple[int, int]], report_date: date) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not ranges:
        return (), ()
    earliest = datetime.fromtimestamp(ranges[0][0], CANONICAL_BUSINESS_TIMEZONE).date()
    latest = min(report_date - timedelta(days=1), datetime.fromtimestamp(ranges[-1][1], CANONICAL_BUSINESS_TIMEZONE).date())
    complete, excluded = [], []
    day = earliest
    while day <= latest:
        start = int(datetime.combine(day, time(), CANONICAL_BUSINESS_TIMEZONE).timestamp())
        end = int(datetime.combine(day + timedelta(days=1), time(), CANONICAL_BUSINESS_TIMEZONE).timestamp())
        # Require the full half-open business day, conservatively including its
        # end boundary. API ranges measured in seconds may overlap at boundaries.
        (complete if any(a <= start and b >= end for a, b in ranges) else excluded).append(day.isoformat())
        day += timedelta(days=1)
    return tuple(complete), tuple(excluded)


def load_daily_fbs_demand(
    observer_db_path: Path, *, report_date: date, nm_ids: Iterable[int],
    warehouse_facility_map: Mapping[int, str],
) -> FbsDemandHistory:
    requested = tuple(map(int, nm_ids))
    try:
        return _load_daily_fbs_demand(observer_db_path, report_date=report_date,
            nm_ids=requested, warehouse_facility_map=warehouse_facility_map)
    except (sqlite3.Error, OSError) as exc:
        empty = {facility: {nm: {} for nm in requested} for facility in set(warehouse_facility_map.values())}
        return FbsDemandHistory(empty, (), (), {'status': 'unavailable', 'source': 'fbs_observer',
            'fingerprint': '', 'reason': type(exc).__name__})


def _load_daily_fbs_demand(
    observer_db_path: Path, *, report_date: date, nm_ids: Iterable[int],
    warehouse_facility_map: Mapping[int, str],
) -> FbsDemandHistory:
    requested = set(map(int, nm_ids))
    empty = {facility: {nm: {} for nm in requested} for facility in set(warehouse_facility_map.values())}
    unavailable = FbsDemandHistory(empty, (), (), {'status': 'unavailable', 'source': 'fbs_observer', 'fingerprint': ''})
    if not Path(observer_db_path).is_file():
        return unavailable
    with closing(sqlite3.connect(Path(observer_db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'observer_runs', OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE, STATUS_OBSERVATIONS_TABLE} <= tables:
            return unavailable
        receipts = []
        for row in conn.execute('SELECT result_json FROM observer_runs ORDER BY rowid'):
            try:
                receipt = json.loads(row[0])
                if isinstance(receipt, dict):
                    receipts.append(receipt)
            except (ValueError, TypeError):
                continue
        ranges = _certified_ranges(receipts)
        complete, excluded = _complete_days(ranges, report_date)
        samples = {f: {nm: {day: 0.0 for day in complete} for nm in requested} for f in empty}
        for f in samples:
            for nm in requested:
                samples[f][nm].update({day: None for day in excluded})
        dispatched = {int(r[0]) for r in conn.execute(f"SELECT DISTINCT order_id FROM {STATUS_OBSERVATIONS_TABLE} WHERE wb_status IN ('sorted','sold','ready_for_pickup','accepted_by_client','defect')")}
        # Seller complete/waiting can precede actual handoff. If later canceled,
        # that uncertain interval must not be called definitely pre/post dispatch.
        delivery_declared = {int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT order_id FROM {STATUS_OBSERVATIONS_TABLE} WHERE supplier_status='complete'")}
        complete_set = set(complete)
        bad_status = 0
        unknown_warehouse_orders = 0
        for row in conn.execute(f'''SELECT o.order_id,o.source_created_at,o.warehouse_id,o.nm_id,o.is_zero_order,
                    s.supplier_status,s.wb_status
                FROM {OBSERVATIONS_TABLE} o LEFT JOIN {STATUS_CURRENT_TABLE} s ON s.order_id=o.order_id
                WHERE o.observation_sequence=(SELECT MAX(x.observation_sequence) FROM {OBSERVATIONS_TABLE} x WHERE x.order_id=o.order_id)'''):
            nm = int(row['nm_id'])
            if nm not in requested or row['is_zero_order']:
                continue
            try:
                day = business_date_from_timestamp(row['source_created_at'])
            except (ValueError, TypeError):
                # Unlocatable requested demand can affect any covered day.
                for f in samples:
                    for covered_day in complete:
                        samples[f][nm][covered_day] = None
                continue
            if day not in complete_set:
                continue
            facility = warehouse_facility_map.get(row['warehouse_id'])
            if facility is None:
                # A warehouse without an authoritative mapping is never assigned
                # to Moscow by the inbound shipment fallback.
                unknown_warehouse_orders += 1
                continue
            supplier, wb = row['supplier_status'], row['wb_status']
            known = supplier in KNOWN_SUPPLIER_STATUSES and wb in KNOWN_WB_STATUSES
            sent = int(row['order_id']) in dispatched
            cancellation = supplier == 'cancel' or wb in {'canceled', 'canceled_by_client', 'declined_by_client'}
            ambiguous_cancel = not sent and (wb in {'canceled_by_client', 'declined_by_client'}
                or (cancellation and int(row['order_id']) in delivery_declared))
            if not known or ambiguous_cancel:
                samples[facility][nm][day] = None
                bad_status += 1
            elif not sent and (supplier == 'cancel' or wb == 'canceled'):
                continue
            elif samples[facility][nm][day] is not None:
                samples[facility][nm][day] += 1.0
        fingerprint_payload = {'receipts': [(r.get('run_id'), r.get('completed_at')) for r in receipts],
            'max_order_revision': conn.execute(f'SELECT MAX(observation_sequence) FROM {OBSERVATIONS_TABLE}').fetchone()[0],
            'max_status_revision': conn.execute(f'SELECT MAX(observation_sequence) FROM {STATUS_OBSERVATIONS_TABLE}').fetchone()[0],
            'mapping': sorted(warehouse_facility_map.items()), 'report_date': report_date.isoformat()}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode()).hexdigest()
    return FbsDemandHistory(samples, complete, excluded, {'status': 'available' if complete else 'unavailable',
        'source': 'fbs_observer', 'fingerprint': fingerprint, 'complete_day_count': len(complete),
        'earliest_available_date': complete[0] if complete else '', 'latest_available_date': complete[-1] if complete else '',
        'excluded_day_count': len(excluded), 'ambiguous_status_order_count': bad_status,
        'unmapped_warehouse_order_count': unknown_warehouse_orders, 'certified_ranges': ranges})


def estimate_fbs_demand(samples: Mapping[str, float | None], *, report_date: date,
                        sales_avg_period_days: int, date_from: date | None = None,
                        date_to: date | None = None) -> FbsDemandEstimate:
    if sales_avg_period_days <= 0:
        raise ValueError('sales_avg_period_days must be positive')
    eligible = [(day, value) for day, value in sorted(samples.items(), reverse=True)
                if date.fromisoformat(day) < report_date
                and (date_from is None or date.fromisoformat(day) >= date_from)
                and (date_to is None or date.fromisoformat(day) <= date_to)]
    baseline_samples = [float(value) for _, value in eligible if value is not None and value > 0][:sales_avg_period_days]
    baseline = float(median(baseline_samples)) if baseline_samples else 0.0
    threshold = max(1.0, baseline * 0.45)
    used, excluded, incomplete, values = [], [], [], []
    for day, value in eligible:
        if len(used) >= sales_avg_period_days:
            break
        if value is None:
            incomplete.append(day)
        elif value < threshold:
            excluded.append(day)
        else:
            used.append(day)
            values.append(float(value))
    warning = '' if len(used) >= sales_avg_period_days else f'Собрано {len(used)} достоверных торговых дней из {sales_avg_period_days}.'
    notes = ['latest_positive_days_frozen_threshold', 'complete_history_backfill_no_calendar_cap']
    if incomplete:
        notes.append('incomplete_days_skipped')
    if not used:
        notes.append('demand_unknown')
    return FbsDemandEstimate(sum(values) / len(values) if values else None,
        tuple(sorted(used)), tuple(sorted(excluded)), tuple(sorted(incomplete)), baseline, threshold, warning, tuple(notes))
