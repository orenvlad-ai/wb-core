"""Persisted planning snapshot and compact stock forecast, independent of accounting.

Only ``refresh_snapshot`` scans history. HTTP reads only the saved compact inputs
and projects visible dates. Sources are opened query-only; the only writes are
atomic files in this module's cache directory.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import time
import threading
from typing import Any, Mapping
from uuid import uuid4

from packages.business_time import current_business_date_iso
from packages.application.fbs_demand_history import load_daily_fbs_demand, estimate_fbs_demand
from packages.application.official_fbs_stock_read import current_official_fbs_facilities
from packages.application.stock_catalog_scope import read_stock_catalog_scope
from packages.application.inventory_planning_read_model import (
    _active_wb_snapshot, _wb_items, FUNCTIONAL_ACTIVE_TABLE, WB_SNAPSHOTS_TABLE,
)
from packages.application.wb_fbs_warehouse_registry import _freshness, WAREHOUSE_MAPPINGS_TABLE
from packages.application.supplier_shipment_status import apply_derived_supplier_status
from packages.contracts.supplier_shipments import MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID

STOCK_MAX_AGE_SECONDS = 72 * 3600
SCHEMA_VERSION = 1
SUPPLIERS = 'sheet_vitrina_v1_supplier_shipments'
SUPPLIER_LINES = 'sheet_vitrina_v1_supplier_shipment_lines'
TEMPORAL = 'temporal_source_snapshots'
WB_DEMAND_WARNING = 'Расход по складу WB отдельно не подтверждён; прогноз этой строки недоступен.'


def _read(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (ValueError, TypeError):
        return default


def _quantity(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _quality(estimate, requested):
    return {'qualified_days': len(estimate.used_dates), 'requested_days': requested,
            'first_date': min(estimate.used_dates, default=None),
            'last_date': max(estimate.used_dates, default=None),
            'excluded_days': len(estimate.excluded_dates),
            'incomplete_days': len(estimate.incomplete_dates), 'warning': estimate.warning}


def read_national_samples(conn, *, nm_ids, report_date):
    """Explicit orderCount rows from successful closed exact-date snapshots.

    Missing identities and invalid/duplicate values remain missing. The accepted
    source's success contract certifies its rows, never absent SKU zeroes.
    """
    samples = {nm: {} for nm in nm_ids}
    if TEMPORAL not in _tables(conn):
        return samples
    for row in conn.execute(
            f"SELECT snapshot_date,captured_at,payload_json FROM {TEMPORAL} "
            "WHERE source_key='sales_funnel_history' AND snapshot_date<? ORDER BY snapshot_date",
            (report_date.isoformat(),)):
        day = row['snapshot_date']
        payload = _json(row['payload_json'], {})
        try:
            closed_capture = current_business_date_iso(datetime.fromisoformat(
                row['captured_at'].replace('Z', '+00:00'))) > day
        except (ValueError, TypeError):
            closed_capture = False
        if (payload.get('kind') != 'success' or not closed_capture or
                payload.get('date_from') != day or payload.get('date_to') != day):
            continue
        observed = {}
        for item in payload.get('items') or []:
            if not isinstance(item, dict) or item.get('metric') != 'orderCount' or item.get('date') != day:
                continue
            nm = item.get('nm_id')
            if nm not in samples:
                continue
            value = _quantity(item.get('value'))
            observed[nm] = None if nm in observed else value
        for nm, value in observed.items():
            samples[nm][day] = value
    return samples


def read_expected_shipments(conn, *, today, moscow_facility_id, delivery_days=30):
    """Calculate remaining inbound by factual departure or supplier planned date."""
    events, warnings = [], []
    tables = _tables(conn)
    if not {SUPPLIERS, SUPPLIER_LINES} <= tables:
        return events, ['Реестр ожидаемых поставок недоступен.']
    for raw in conn.execute(f'SELECT * FROM {SUPPLIERS} ORDER BY shipment_id'):
        header = apply_derived_supplier_status(dict(raw), business_today=today.isoformat())
        if header.get('archived_at') or header.get('actual_ff_acceptance_date'):
            continue
        status = header.get('order_status')
        if status not in {'production', 'in_transit'}:
            continue
        shipment_id = header['shipment_id']
        label = str(header.get('invoice_no') or shipment_id)
        anchor = header.get('actual_shipment_date') if status == 'in_transit' else header.get('shipment_date')
        try:
            departure = date.fromisoformat(str(anchor))
            arrival = departure + timedelta(days=delivery_days)
        except (ValueError, TypeError, OverflowError):
            warnings.append(f'{label}: нет корректной даты отгрузки; поступление не включено.')
            continue
        if arrival < today:
            warnings.append(f'{label}: расчётная дата поступления {arrival.isoformat()} прошла; поставка не включена.')
            continue
        shipment_warnings = []
        if status == 'production' and departure < today:
            shipment_warnings.append(f'{label}: плановая отгрузка просрочена, поступление пока по плану.')
        target = str(header.get('target_facility_id') or '').strip() or moscow_facility_id
        if not target:
            warnings.append(f'{label}: склад Москва не найден; складской прогноз поступления недоступен.')
        quantities = {}
        for line in conn.execute(f'SELECT * FROM {SUPPLIER_LINES} WHERE shipment_id=? ORDER BY sort_order,line_id', (shipment_id,)):
            if line['line_type'] != 'product' or line['match_status'] not in MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID:
                continue
            nm, quantity = line['internal_nm_id'], _quantity(line['qty'])
            if not isinstance(nm, int) or nm <= 0 or quantity is None or quantity <= 0:
                continue
            quantities[nm] = quantities.get(nm, 0) + quantity
        # Existing supplier receipts can be partial. Net source-linked quantities
        # are subtracted once, before emitting SKU events; fully received headers
        # have already been excluded above.
        if {'sheet_vitrina_v1_ff_stock_operations', 'sheet_vitrina_v1_ff_stock_operation_lines'} <= tables:
            accepted = conn.execute("""SELECT line.nm_id,SUM(line.quantity_delta) quantity
                FROM sheet_vitrina_v1_ff_stock_operations operation
                JOIN sheet_vitrina_v1_ff_stock_operation_lines line USING(operation_id)
                WHERE operation.source_type IN ('supplier_shipment_acceptance','supplier_shipment_acceptance_recovery') AND operation.source_object_id=?
                GROUP BY line.nm_id""", (shipment_id,))
            for row in accepted:
                if row['nm_id'] in quantities:
                    quantities[row['nm_id']] = max(0, quantities[row['nm_id']] - max(0, row['quantity'] or 0))
        for nm, quantity in quantities.items():
            if quantity <= 0:
                continue
            events.append({'nm_id': nm, 'shipment_id': shipment_id, 'label': label,
                'quantity': quantity, 'status': status, 'arrival_date': arrival.isoformat(),
                'target_facility_id': target,
                'target_assignment_source': 'explicit' if header.get('target_facility_id') else 'default_fbs_moscow',
                'warnings': shipment_warnings})
        warnings.extend(shipment_warnings)
    return events, list(dict.fromkeys(warnings))


def first_deficit_date(*, today, quantity, daily_demand, inbounds):
    """Find the first zero day event-wise, including beyond the visible window."""
    if quantity is None or daily_demand is None:
        return None
    supply = {}
    for item in inbounds:
        day = date.fromisoformat(item['arrival_date'])
        if day >= today:
            supply[day] = supply.get(day, 0) + float(item['quantity'])
    stock, cursor = float(quantity) + supply.pop(today, 0), today
    if stock <= 0:
        return today.isoformat()
    if daily_demand <= 0:
        return None
    for arrival, incoming in sorted(supply.items()):
        days = (arrival - cursor).days
        exhaustion = math.ceil(stock / daily_demand)
        # An arrival at the start of the zero day rescues that day.
        if exhaustion < days:
            return (cursor + timedelta(days=exhaustion)).isoformat()
        stock = max(0, stock - days * daily_demand) + incoming
        cursor = arrival
    exhaustion = math.ceil(stock / daily_demand)
    try:
        return (cursor + timedelta(days=exhaustion)).isoformat()
    except OverflowError:
        return None


def forecast_cells(*, today, horizon_days, quantity, daily_demand, inbounds, date_offset=0):
    arrivals = {}
    for event in inbounds:
        if event['arrival_date'] >= today.isoformat():
            arrivals.setdefault(event['arrival_date'], []).append(event)
    window_start = today + timedelta(days=date_offset)
    stock = quantity
    if date_offset and stock is not None:
        if daily_demand is None:
            stock = None
        else:
            cursor = today
            for arrival, events in sorted(arrivals.items()):
                arrived = date.fromisoformat(arrival)
                if arrived >= window_start:
                    break
                stock = max(0, stock - (arrived - cursor).days * daily_demand) + sum(e['quantity'] for e in events)
                cursor = arrived
            stock = max(0, stock - (window_start - cursor).days * daily_demand)
    cells = []
    for offset in range(horizon_days):
        day = (window_start + timedelta(days=offset)).isoformat()
        shipments = arrivals.get(day, [])
        if stock is not None:
            if offset and daily_demand is not None:
                stock = max(0, stock - daily_demand)
            if offset and daily_demand is None:
                stock = None
            if stock is not None:
                stock += sum(event['quantity'] for event in shipments)
        cover = stock / daily_demand if stock is not None and daily_demand and daily_demand > 0 else None
        color = ('unknown' if stock is None else 'red' if stock == 0 else
                 'unknown' if cover is None else 'green' if cover >= 30 else
                 'yellow' if cover >= 14 else 'orange' if cover >= 7 else 'red')
        cells.append({'date': day, 'quantity': round(stock, 3) if stock is not None else None,
            'cover_days': round(cover, 3) if cover is not None else None,
            'bar_ratio': min(1, cover / 30) if cover is not None else None,
            'color': color, 'inbounds': shipments})
    return cells


class StockMonitorService:
    def __init__(self, *, runtime, now_factory=None, cache_dir=None, observer_db_path=None, ads_loader=None):
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.cache_dir = Path(cache_dir or (Path(runtime.runtime_dir) / 'stock_monitor'))
        self.observer_db_path = Path(observer_db_path or (Path(runtime.runtime_dir) / 'fbs_observer' / 'observations.sqlite3'))
        self._ads_loader = ads_loader
        self._market_ads = None
        self._market_ads_read = None
        self._market_ads_read_at = None
        self._market_ads_failed = False
        self._market_scope = threading.local()

    @contextmanager
    def market_refresh_scope(self):
        """One lazy reference batch for all periods in an owned refresh operation."""
        existing = getattr(self._market_scope, 'batch', None)
        if existing is not None:
            yield
            return
        self._market_scope.batch = {'attempted': False}
        try:
            yield
        finally:
            del self._market_scope.batch

    def _load_market_ads(self):
        monotonic = time.monotonic()
        scope = getattr(self._market_scope, 'batch', None)
        if scope is not None and scope['attempted']:
            if scope.get('failed'):
                raise RuntimeError('market_ads_batch_unavailable')
            return scope['read']
        if scope is None and self._market_ads_read_at is not None and monotonic - self._market_ads_read_at < 120:
            if self._market_ads_failed:
                raise RuntimeError('market_ads_batch_unavailable')
            return self._market_ads_read
        try:
            if self._ads_loader is not None:
                read = self._ads_loader()
            else:
                if self._market_ads is None:
                    from packages.application.sheet_vitrina_v1_ads import SheetVitrinaV1AdsBlock
                    self._market_ads = SheetVitrinaV1AdsBlock(runtime=self.runtime,
                        runtime_dir=Path(self.runtime.runtime_dir), now_factory=self.now_factory,
                        timestamp_factory=lambda: self.now_factory().isoformat(), cache_ttl_seconds=120)
                read = self._market_ads.build_placement_index_read(bypass_cache=scope is not None)
                # The existing batch cache retains the actual timestamp even
                # for an empty index. No additional request is made here.
                payload = self._market_ads._campaign_cache['payload']
                read['captured_at'] = payload['fetched_at']
                # The index omits unknown bids; preserve membership from this
                # same batch so a missing second campaign/placement is visible.
                read['campaigns'] = payload['campaigns']
            self._market_ads_read, self._market_ads_failed = read, False
            return read
        except Exception:
            self._market_ads_failed = True
            raise
        finally:
            # A failed batch is also reused between the default/preferred N;
            # do not duplicate a provider request during one cycle refresh.
            self._market_ads_read_at = monotonic
            if scope is not None:
                scope.update(attempted=True, failed=self._market_ads_failed, read=self._market_ads_read)

    def _enrich_market(self, base, previous):
        from packages.application.stock_monitor_market import build_market, FIELDS
        old = {row['nm_id']: row.get('market', {}) for row in previous.get('rows', [])}
        try:
            market = build_market(self.runtime, nm_ids=[r['nm_id'] for r in base['rows']],
                today=base['report_date'], previous=old, ads_loader=self._load_market_ads)
        except Exception:
            market = {}
            for row in base['rows']:
                market[row['nm_id']] = {field: {'value': None, 'captured_at': None,
                    'status': 'missing', 'source': source, 'warning': 'Справочные данные не обновились.'}
                    for field, source in FIELDS.items()}
                for field, cell in old.get(row['nm_id'], {}).items():
                    if field in FIELDS and field != 'buyer_wallet_price' and cell.get('value') is not None:
                        market[row['nm_id']][field] = {**cell, 'status': 'stale', 'warning': 'Справочные данные не обновились.'}
        for row in base['rows']:
            row['market'] = market[row['nm_id']]

    @staticmethod
    def _period(value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError('Период усреднения должен быть положительным целым числом.')
        return value

    def _path(self, period_days):
        return self.cache_dir / f'period-{self._period(period_days)}.json'

    @contextmanager
    def _lock(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.cache_dir / 'refresh.lock').open('a') as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def refresh_snapshot(self, *, period_days=14):
        """Cycle/explicit refresh only. Failure cannot overwrite the previous file."""
        path = self._path(period_days)
        attempt_path = path.with_name(path.stem + '-attempt.json')
        with self._lock():
            try:
                base = self._build_base(period_days)
                try:
                    previous = json.loads(path.read_text())
                except (OSError, ValueError):
                    previous = {}
                if not previous and period_days != 14:
                    try:
                        previous = json.loads(self._path(14).read_text())
                    except (OSError, ValueError):
                        pass
                try:
                    self._enrich_market(base, previous)
                except Exception:
                    # Reference enrichment must never prevent stock publication.
                    for row in base['rows']:
                        row['market'] = {}
                base['source_fingerprint'] = hashlib.sha256(json.dumps(
                    {'stock': base['source_fingerprint'], 'market': [row['market'] for row in base['rows']]},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                _atomic_json(path, base)
            except Exception as exc:
                try:
                    _atomic_json(attempt_path, {'status': 'failed',
                        'attempted_at': self.now_factory().isoformat(), 'error_code': type(exc).__name__})
                except OSError:
                    pass
                raise
            _atomic_json(attempt_path, {'status': 'published', 'attempted_at': base['generated_at']})
        return {'status': base['status'], 'period_days': period_days,
                'generated_at': base['generated_at'], 'sku_count': len(base['rows']),
                'source_fingerprint': base['source_fingerprint']}

    def _build_base(self, period_days):
        now = self.now_factory()
        today = date.fromisoformat(current_business_date_iso(now))
        with closing(_read(self.runtime.db_path)) as conn:
            scope = read_stock_catalog_scope(conn)
            if not scope['complete']:
                raise ValueError('Каталог SKU неполон; предыдущий снимок мониторинга сохранён.')
            items = scope['items']
            nm_ids = scope['nm_ids']
            national = read_national_samples(conn, nm_ids=nm_ids, report_date=today)
            tables = _tables(conn)
            wb = _active_wb_snapshot(conn) if {FUNCTIONAL_ACTIVE_TABLE, WB_SNAPSHOTS_TABLE} <= tables else None
            wb_source = {'captured_at': wb['fetched_at'], 'date': wb['snapshot_date'],
                'snapshot_id': wb['snapshot_id'], 'digest': wb['raw_rows_digest']} if wb is not None else {}
            wb_usable = wb is not None and bool(wb['pagination_complete']) and _source_admitted(wb_source, now)
            wb_values = {item['nm_id']: item['quantity'] for item in _wb_items(wb)} if wb_usable else {}
            warehouse_map = {int(row['seller_warehouse_id']): str(row['facility_id'])
                for row in conn.execute(f'SELECT seller_warehouse_id,facility_id FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1')} if WAREHOUSE_MAPPINGS_TABLE in tables else {}
            official = current_official_fbs_facilities(self.runtime.db_path,
                requested_nm_ids=nm_ids, now=now, planning_max_age_seconds=STOCK_MAX_AGE_SECONDS)
            facilities = official.get('facilities') or []
            if not facilities:
                raise ValueError('Официальный источник складов FBS недоступен; предыдущий снимок мониторинга сохранён.')
            categories = {row['group_key']: row['label'] for row in
                conn.execute('SELECT group_key,label FROM sheet_vitrina_v1_sku_groups')} if 'sheet_vitrina_v1_sku_groups' in tables else {}
            moscow = next((f['facility_id'] for f in facilities if 'москва' in (str(f.get('city', '')) + ' ' + str(f.get('name', ''))).casefold()), '')
            delivery_days = 30
            settings_source = 'defaults'
            table = 'sheet_vitrina_v1_fbs_fulfillment_order_result_state'
            if table in tables:
                saved = conn.execute(f'SELECT result_json FROM {table} WHERE slot=1').fetchone()
                settings = _json(saved[0], {}).get('settings', {}) if saved is not None else {}
                value = _quantity(settings.get('factory_to_target_ff_days'))
                if value is not None and value.is_integer() and value <= 36500:
                    delivery_days, settings_source = int(value), 'last_fbs_fulfillment_order_settings'
            inbounds, warnings = read_expected_shipments(conn, today=today,
                moscow_facility_id=moscow, delivery_days=delivery_days)
        history = load_daily_fbs_demand(self.observer_db_path, report_date=today,
            nm_ids=nm_ids, warehouse_facility_map=warehouse_map)
        rows = []
        for item in items:
            nm = item['nm_id']
            children = []
            for facility in facilities:
                values = {v['nm_id']: v['available'] for v in facility['sku_values']}
                estimate = estimate_fbs_demand(history.samples(facility['facility_id'], nm),
                    report_date=today, sales_avg_period_days=period_days)
                children.append({'facility_id': facility['facility_id'], 'name': str(facility['name']).replace('FF ', 'FBS '),
                    'current_stock': values.get(nm), 'avg_daily_sales': estimate.daily_demand,
                    'demand_source': 'fbs_warehouse_orders', 'demand_quality': _quality(estimate, period_days),
                    'stock_source': facility['stock_source'],
                    'warnings': list(filter(None, [facility.get('source_blocker'),
                        facility['stock_source'].get('warning'), estimate.warning]))})
            children.append({'facility_id': 'wb', 'name': 'Склад WB', 'current_stock': wb_values.get(nm),
                'avg_daily_sales': None, 'demand_source': 'unavailable', 'stock_source': wb_source,
                'warnings': [WB_DEMAND_WARNING] + ([] if wb_values.get(nm) is not None else ['Нет полного свежего снимка остатков WB для SKU.'])})
            quantities = [child['current_stock'] for child in children]
            estimate = estimate_fbs_demand(national[nm], report_date=today, sales_avg_period_days=period_days)
            row_warnings = list(filter(None, [estimate.warning, official.get('warning')]))
            if any(value is None for value in quantities):
                row_warnings.append('Общий остаток недоступен: нет полного допустимого снимка одного из складов.')
            rows.append({'nm_id': nm, 'name': _sku_name(item),
                'category': categories.get(item.get('product_type'), str(item.get('product_type') or item.get('wb_subject_name') or '')),
                'current_stock': sum(quantities) if all(value is not None for value in quantities) else None,
                'avg_daily_sales': estimate.daily_demand, 'demand_source': 'national_orderCount',
                'demand_quality': _quality(estimate, period_days), 'warnings': row_warnings,
                'warehouses': children})
        if self._path(period_days).is_file() and (
                not any(w['current_stock'] is not None for r in rows for w in r['warehouses']) or
                not any(r['avg_daily_sales'] is not None or any(w['avg_daily_sales'] is not None for w in r['warehouses']) for r in rows)):
            raise ValueError('Источники остатков или спроса недоступны; предыдущий снимок мониторинга сохранён.')
        material = {'rows': rows, 'inbounds': inbounds, 'catalog_scope': scope['scope_digest'],
                    'observer': history.coverage, 'delivery_days': delivery_days}
        fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {'schema_version': SCHEMA_VERSION, 'contract_name': 'stock_monitor',
            'contract_version': SCHEMA_VERSION, 'status': 'ready' if all(r['current_stock'] is not None and r['avg_daily_sales'] is not None for r in rows) else 'partial',
            'generated_at': now.isoformat(), 'report_date': today.isoformat(), 'period_days': period_days,
            'rows': rows, 'inbounds': inbounds, 'warnings': warnings,
            'source_fingerprint': fingerprint,
            'delivery_days': delivery_days, 'delivery_settings_source': settings_source,
            'freshness': {'fbs_captured_at': official.get('captured_at'), 'wb_captured_at': wb_source.get('captured_at')},
            'fbs_coverage': history.coverage,
            'formula': {'stock_source': 'official_snapshots', 'old_stock_adjusted': False,
                'aggregate_demand': 'national_orderCount_independent_of_FBS',
                'warehouse_wb_demand_available': False, 'stock_max_age_hours': 72,
                'lost_sales_carried_as_debt': False, 'arrival_at_day_start': True}}

    def get_snapshot(self, *, period_days=14, horizon_days=60, date_offset=0, view_days=90):
        """Read saved inputs and project visible dates; never fetch/scan/rebuild."""
        self._period(period_days)
        if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or not 1 <= horizon_days <= 3650:
            raise ValueError('Горизонт должен быть от 1 до 3650 дней.')
        if isinstance(date_offset, bool) or not isinstance(date_offset, int) or not 0 <= date_offset < horizon_days:
            raise ValueError('Смещение дат должно находиться внутри горизонта.')
        if isinstance(view_days, bool) or not isinstance(view_days, int) or not 1 <= view_days <= 90:
            raise ValueError('Окно отображения должно быть от 1 до 90 дней.')
        visible_days = min(view_days, horizon_days - date_offset)
        now = self.now_factory()
        today = date.fromisoformat(current_business_date_iso(now))
        common = {'contract_name': 'stock_monitor', 'contract_version': SCHEMA_VERSION,
            'report_date': today.isoformat(), 'period_days': period_days, 'horizon_days': horizon_days,
            'date_offset': date_offset, 'visible_days': visible_days,
            'dates': [(today + timedelta(days=i)).isoformat() for i in range(date_offset, date_offset + visible_days)]}
        path = self._path(period_days)
        try:
            refresh = json.loads(path.with_name(path.stem + '-attempt.json').read_text())
        except (OSError, ValueError, TypeError):
            refresh = {}
        try:
            base = json.loads(path.read_text())
            if base.get('schema_version') != SCHEMA_VERSION or base.get('period_days') != period_days:
                raise ValueError('cache_contract_mismatch')
        except (OSError, ValueError, TypeError):
            return {**common, 'status': 'unavailable', 'rows': [], 'warnings': ['Снимок мониторинга ещё не рассчитан.'], 'cache': {'hit': False}, 'refresh': refresh}
        warnings = list(base['warnings'])
        if refresh.get('status') == 'failed':
            warnings.append('Последнее обновление мониторинга не удалось; показан предыдущий снимок.')
        if base['report_date'] != today.isoformat():
            warnings.append('Расчёт спроса сохранён ранее; ожидается обновление снимка мониторинга.')
        inbounds = [event for event in base['inbounds'] if event['arrival_date'] >= today.isoformat()]
        if len(inbounds) != len(base['inbounds']):
            warnings.append('Просроченные неподтверждённые поступления исключены из прогноза.')
        rows = []
        for saved in base['rows']:
            row = dict(saved)
            children = []
            for stored in saved['warehouses']:
                child = dict(stored)
                child['warnings'] = list(stored['warnings'])
                if not _source_admitted(child['stock_source'], now):
                    child['current_stock'] = None
                    child['warnings'].append('Снимок остатков недоступен или старше 72 часов.')
                elif _source_age(child['stock_source'], now) > 9 * 3600:
                    child['warnings'].append('Снимок старше 9 часов; количество с даты снимка не корректировалось.')
                child_events = [event for event in inbounds if event['nm_id'] == row['nm_id'] and event['target_facility_id'] == child['facility_id']]
                children.append(_project_row(child, today=today, horizon_days=visible_days, date_offset=date_offset, inbounds=child_events))
            row['warehouses'] = children
            values = [child['current_stock'] for child in children]
            row['current_stock'] = sum(values) if all(v is not None for v in values) else None
            if any(_source_age(c['stock_source'], now) > 9 * 3600 for c in children):
                row['warnings'] = [*row['warnings'], 'Остатки одного из складов сняты более 9 часов назад; количество не корректировалось.']
            if row['current_stock'] is None:
                row['warnings'] = [*row['warnings'], 'Общий прогноз недоступен: остатки одного из складов не подтверждены.']
            row_events = [event for event in inbounds if event['nm_id'] == row['nm_id']]
            rows.append(_project_row(row, today=today, horizon_days=visible_days, date_offset=date_offset, inbounds=row_events))
        rows.sort(key=lambda r: (r['forecast_state'] == 'ready', r['deficit_date'] or '9999-12-31', r['name'].casefold()))
        return {**base, **common, 'rows': rows, 'warnings': list(dict.fromkeys(warnings)),
            'status': 'ready' if all(r['forecast_state'] == 'ready' for r in rows) else 'partial',
            'refresh': refresh, 'cache': {'hit': True, 'source_fingerprint': base['source_fingerprint'],
                'age_seconds': max(0, int((now - datetime.fromisoformat(base['generated_at'])).total_seconds()))}}


def _atomic_json(path, payload):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sku_name(item):
    name = str(item.get('our_sku') or item.get('nomenclature_name') or item['nm_id']).strip()
    tokens = str(item.get('product_type') or '').split('_')
    # Category already has its own column. Remove only the exact anchored
    # category prefix, retaining SKU/model text and any nonmatching name.
    pattern = r'^\s*\(?\s*' + r'[\s_-]+'.join(re.escape(t) for t in tokens if t) + r'\s*\)?(?=\s|$)\s*'
    compact = re.sub(pattern, '', name, count=1, flags=re.IGNORECASE) if tokens and tokens[0] else name
    return compact or name


def _source_age(source, now):
    try:
        return (now - datetime.fromisoformat(source['captured_at'].replace('Z', '+00:00'))).total_seconds()
    except (KeyError, TypeError, ValueError):
        return math.inf


def _source_admitted(source, now):
    age = _source_age(source, now)
    return -300 <= age <= STOCK_MAX_AGE_SECONDS


def _project_row(row, *, today, horizon_days, inbounds, date_offset=0):
    quantity, demand = row['current_stock'], row['avg_daily_sales']
    deficit = first_deficit_date(today=today, quantity=quantity, daily_demand=demand, inbounds=inbounds)
    return {**row, 'forecast_state': 'ready' if quantity is not None and demand is not None else 'unavailable',
        'warnings': list(dict.fromkeys(row['warnings'])), 'deficit_date': deficit,
        'deficit_days': (date.fromisoformat(deficit) - today).days if deficit else None,
        'cells': forecast_cells(today=today, horizon_days=horizon_days, quantity=quantity,
            daily_demand=demand, inbounds=inbounds, date_offset=date_offset)}
