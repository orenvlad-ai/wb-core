"""Dormant finite old-date source→ready→native obligation, owned by a cycle.

One private receipt, at most two dates. No scheduler, worker launcher, source
resend after an uncertain attempt, schema bootstrap or new edition contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta
import json
import os
from pathlib import Path
import stat
import tempfile

from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.business_data_heavy_admission import require_heavy_owner
from packages.application.ready_publication import canonical, digest, readonly, capture_build_inputs, capture_authority

FILENAME = 'sheet-vitrina-closed-backlog.json'
MAX_BYTES = 64 * 1024
MAX_SOURCES = 32


class ClosedBacklogConflict(ValueError):
    """A specific old operand/publication is unavailable or changed."""


def require_cycle_owner(runtime):
    if require_heavy_owner(runtime.runtime_dir).operation != 'cycle':
        raise RuntimeError('closed_backlog_requires_cycle_owner')


def accepted_cached_slot(key, status, payload, day):
    """Retain canonical qualified empty ads, partial sources and Finance proof."""
    diagnostics = live._payload_diagnostics(payload) if payload is not None else {}
    if key == 'ads_compact' and status.kind == 'empty':
        return bool(payload is not None and live._resolve_freshness(payload) == day
            and diagnostics.get('completeness_state') == 'complete'
            and diagnostics.get('no_activity_proven') is True
            and diagnostics.get('dated_roster_state') == 'caller_qualified_dated_roster')
    accepted = live._is_valid_temporal_candidate(source_key=key, status=status, payload=payload,
        column_date=day, temporal_slot=live.TEMPORAL_SLOT_YESTERDAY_CLOSED)
    if key == 'fin_report_daily':
        pagination = (status.diagnostics or {}).get('pagination', {})
        accepted = accepted and pagination.get('complete') is True and pagination.get('terminal_status') == 204
    return accepted


class ClosedBacklog:
    def __init__(self, sources):
        self.sources = sources
        self.runtime = sources.block.runtime
        self.path = Path(self.runtime.runtime_dir) / FILENAME

    def status(self):
        """Bounded metadata only; never provision, reconcile or fetch on status."""
        if not self.path.exists():
            return None
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                observed = os.fstat(stream.fileno())
                if not stat.S_ISREG(observed.st_mode) or observed.st_mode & 0o077 or observed.st_size > MAX_BYTES:
                    raise ClosedBacklogConflict('closed_receipt_unsafe')
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ClosedBacklogConflict('closed_receipt_size')
            value = json.loads(raw)
            if (value['schema_version'] != 1 or not isinstance(value['dates'], dict)
                    or len(value['dates']) > 2 or not isinstance(value['authority'], dict)
                    or len(value['cycle_id']) != 32 or any(c not in '0123456789abcdef' for c in value['cycle_id'])
                    or not value['bundle_version']):
                raise ClosedBacklogConflict('closed_receipt_invalid')
            if ('collection_bundle_version' in value and
                    (type(value['collection_bundle_version']) is not str or not value['collection_bundle_version'])):
                raise ClosedBacklogConflict('closed_receipt_invalid')
            count = 0
            for day, item in value['dates'].items():
                date.fromisoformat(day)
                if item['state'] not in {'planned', 'capturing', 'pending', 'accepted', 'ready', 'acknowledged'}:
                    raise ClosedBacklogConflict('closed_receipt_invalid')
                if not isinstance(item['sources'], dict) or not item['sources']:
                    raise ClosedBacklogConflict('closed_receipt_invalid')
                if ('deferred_reason' in item and (type(item['deferred_reason']) is not str or not item['deferred_reason'])
                        or 'previous_ready' in item and not isinstance(item['previous_ready'], dict)):
                    raise ClosedBacklogConflict('closed_receipt_invalid')
                count += len(item['sources'])
                for key, source in item['sources'].items():
                    if key not in live.SOURCE_TEMPORAL_POLICIES or key in {live.OWN_PRODUCT_CAPITAL_SOURCE_KEY, 'cost_price', live.SKU_ACTION_SOURCE_KEY}:
                        raise ClosedBacklogConflict('closed_receipt_invalid')
                    if 'attempt_scope_fingerprint' in source:
                        fingerprint = source['attempt_scope_fingerprint']
                        if (type(fingerprint) is not str or not fingerprint.startswith('sha256:') or len(fingerprint) != 71
                                or any(c not in '0123456789abcdef' for c in fingerprint[7:])):
                            raise ClosedBacklogConflict('closed_receipt_invalid')
                    for field in ('anchor', 'before'):
                        proof = source[field]
                        if proof is None: continue
                        role = live.TEMPORAL_ROLE_ACCEPTED_CURRENT if key in live.CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS else live.TEMPORAL_ROLE_ACCEPTED_CLOSED
                        if (set(proof) != {'source_key', 'snapshot_date', 'snapshot_role', 'digest'}
                                or (proof['source_key'], proof['snapshot_date'], proof['snapshot_role']) != (key, day, role)
                                or not proof['digest'].startswith('sha256:') or len(proof['digest']) != 71
                                or any(c not in '0123456789abcdef' for c in proof['digest'][7:])):
                            raise ClosedBacklogConflict('closed_receipt_invalid')
            if count > MAX_SOURCES:
                raise ClosedBacklogConflict('closed_receipt_invalid')
        except (OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
            if isinstance(exc, ClosedBacklogConflict): raise
            raise ClosedBacklogConflict('closed_receipt_invalid') from exc
        return value

    def _write(self, value):
        require_cycle_owner(self.runtime)
        raw = canonical(value).encode()
        if len(raw) > MAX_BYTES:
            raise ClosedBacklogConflict('closed_receipt_size')
        if self.path.is_symlink():
            raise ClosedBacklogConflict('closed_receipt_unsafe')
        fd, temporary = tempfile.mkstemp(prefix='.' + FILENAME, dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _due(self):
        """SQL bounds eligible metadata before applying canonical due semantics."""
        now = self.sources.block.now_factory()
        today = live.current_business_date_iso(now)
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        # Own capital is read/derived locally, never a persisted temporal
        # capture. Its old rows remain part of canonical dated derivation.
        keys = sorted(live.HISTORICAL_CLOSED_DAY_SOURCE_KEYS - {live.OWN_PRODUCT_CAPITAL_SOURCE_KEY})
        placeholders = ','.join('?' for _ in keys)
        where = f'''source_key IN ({placeholders}) AND slot_kind=? AND target_date<? AND
            ((state IN ({','.join('?' for _ in live.CLOSURE_PENDING_STATES)}) AND
               (next_retry_at IS NULL OR julianday(next_retry_at)<=julianday(?))) OR
             (source_key='fin_report_daily' AND state=? AND target_date>='2026-09-12'
               AND last_attempt_at IS NOT NULL AND date(last_attempt_at,'+5 hours')<?))'''
        args = (*keys, live.TEMPORAL_SLOT_YESTERDAY_CLOSED, yesterday,
                *sorted(live.CLOSURE_PENDING_STATES), now.isoformat(), live.CLOSURE_STATE_EXHAUSTED, today)
        with readonly(self.runtime.db_path) as conn:
            days = [row[0] for row in conn.execute('SELECT target_date FROM temporal_source_closure_state WHERE '
                + where + ' GROUP BY target_date ORDER BY MIN(COALESCE(last_attempt_at,\'\')),target_date LIMIT 2', args)]
            if not days:
                return {}
            rows = conn.execute('SELECT * FROM temporal_source_closure_state WHERE ' + where
                + f" AND target_date IN ({','.join('?' for _ in days)}) ORDER BY target_date,source_key LIMIT ?",
                (*args, *days, MAX_SOURCES + 1)).fetchall()
        if len(rows) > MAX_SOURCES:
            raise ClosedBacklogConflict('closed_source_scope_limit')
        result = {day: [] for day in days}
        for row in rows:
            state = live.TemporalSourceClosureState(**dict(row))
            if live._closure_attempt_is_due(state, now):
                result[state.target_date].append(state.source_key)
        return {day: keys for day, keys in result.items() if keys}

    def _anchor(self, key, day, *, connection=None):
        role = (live.TEMPORAL_ROLE_ACCEPTED_CURRENT if key in live.CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS
                else live.TEMPORAL_ROLE_ACCEPTED_CLOSED)
        if connection is None:
            with readonly(self.runtime.db_path) as conn:
                return self._anchor(key, day, connection=conn)
        row = connection.execute('SELECT captured_at,payload_json FROM temporal_source_slot_snapshots '
            'WHERE source_key=? AND snapshot_date=? AND snapshot_role=?', (key, day, role)).fetchone()
        if row is None:
            return None
        return dict(source_key=key, snapshot_date=day, snapshot_role=role, digest=digest(canonical(list(row))))

    def _valid_anchor(self, key, day, nm_ids):
        # Validation and raw anchor belong to one pinned native snapshot. A
        # commit between two independent reads cannot bless different bytes.
        from packages.application.web_vitrina_window_read_context import window_read_context
        with window_read_context(self.runtime.db_path, runtime_dir=self.runtime.runtime_dir):
            return self._valid_anchor_in_snapshot(key, day, nm_ids)

    def _valid_anchor_in_snapshot(self, key, day, nm_ids):
        role = (live.TEMPORAL_ROLE_ACCEPTED_CURRENT if key in live.CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS
                else live.TEMPORAL_ROLE_ACCEPTED_CLOSED)
        cached = self.sources.block._load_slot_snapshot_status(source_key=key,
            temporal_slot=live.TEMPORAL_SLOT_YESTERDAY_CLOSED, temporal_policy=live.SOURCE_TEMPORAL_POLICIES[key],
            column_date=day, requested_nm_ids=list(nm_ids), snapshot_role=role,
            require_closed_day_fresh=role == live.TEMPORAL_ROLE_ACCEPTED_CLOSED)
        if cached is None or not accepted_cached_slot(key, cached[0], cached[1], day):
            return None
        return self._anchor(key, day)

    @staticmethod
    def _attempt_scope(enabled, nm_ids):
        # Actual source request operands, not the publication bundle label.
        return digest(canonical([sorted(set(nm_ids)), sorted({item.group for item in enabled})]))

    def _reconcile_publication_context(self, value, current, enabled):
        """Resolve original effects before rebinding a known publication demand.

        Anchored bytes/authority are immutable. Current-scope cache admission is
        separate: its refusal defers this old date without stopping current work.
        """
        if value['authority'] != capture_authority(self.runtime.runtime_dir, db_path=self.runtime.db_path):
            raise ClosedBacklogConflict('closed_source_authority_changed')
        changed = value['bundle_version'] != current.bundle_version
        uncertain = False
        for day, item in value['dates'].items():
            if item['state'] == 'acknowledged':
                continue
            for key, source in item['sources'].items():
                raw = self._anchor(key, day)
                if source['anchor'] is not None:
                    if raw != source['anchor']:
                        raise ClosedBacklogConflict('closed_accepted_source_changed:' + key)
                    continue
                if source['outcome'] not in {'capturing', 'outcome_unknown'}:
                    continue
                nm_ids = live.require_stock_catalog_scope(self.runtime.db_path)['nm_ids'] if key == 'stocks' else [v.nm_id for v in enabled]
                scope = self._attempt_scope(enabled, nm_ids)
                original_scope = source.get('attempt_scope_fingerprint')
                # A legacy unknown receipt cannot acquire new authority merely
                # because a new registry bundle was registered. No resend.
                if original_scope != scope and (original_scope is not None or changed):
                    raise ClosedBacklogConflict('closed_source_attempt_scope_changed:' + key + ':' + day)
                anchor = self._valid_anchor(key, day, nm_ids)
                state = self.runtime.load_temporal_source_closure_state(source_key=key,
                    target_date=day, slot_kind=live.TEMPORAL_SLOT_YESTERDAY_CLOSED)
                if anchor is not None:
                    source.update(anchor=anchor, outcome='accepted_retained' if anchor == source['before'] else 'accepted',
                                  latest_attempt=asdict(state) if state else None)
                elif state is None or asdict(state) == source['attempt_before']:
                    source['outcome'] = 'outcome_unknown'; uncertain = True
                else:
                    # The canonical attempt receipt makes this known pending;
                    # the existing due rule alone may authorize a later attempt.
                    source['outcome'] = 'attempt_pending'
                    source['latest_attempt'] = asdict(state)
            if item['state'] == 'capturing' and all(v['outcome'] not in {'capturing', 'outcome_unknown'} for v in item['sources'].values()):
                item['state'] = 'accepted' if all(v['anchor'] for v in item['sources'].values()) else 'pending'
        if uncertain:
            self._write(value)
            return False
        value.setdefault('collection_bundle_version', value['bundle_version'])
        value['bundle_version'] = current.bundle_version
        for day, item in value['dates'].items():
            if item['state'] == 'acknowledged':
                continue
            item.pop('deferred_reason', None)
            for key, source in item['sources'].items():
                if source['anchor'] is None:
                    continue
                nm_ids = live.require_stock_catalog_scope(self.runtime.db_path)['nm_ids'] if key == 'stocks' else [v.nm_id for v in enabled]
                admitted = self._valid_anchor(key, day, nm_ids)
                if self._anchor(key, day) != source['anchor']:
                    raise ClosedBacklogConflict('closed_accepted_source_changed:' + key)
                if admitted != source['anchor']:
                    # Raw bytes above still match. This is an admission/scope
                    # refusal, never permission to refetch an accepted operand.
                    item['deferred_reason'] = 'closed_retained_scope_unavailable:' + key + ':' + day
                    break
            if (changed or item.get('deferred_reason')) and item['state'] == 'ready':
                item['previous_ready'] = item.pop('ready')
                item['state'] = 'accepted'
        # Demand, source anchors and original attempt evidence survive a crash
        # after this publication-only transition. No old source is acquired here.
        self._write(value)
        return True

    def collect(self, cycle_id):
        """Reconcile the sole prior obligation before enrolling a new finite batch.

        Canonical accepted rows discharge acquisition, never publication. An
        uncertain missing acquisition only resumes after its original canonical
        closure receipt proves an attempt and the existing due rule permits it.
        """
        require_cycle_owner(self.runtime)
        if len(cycle_id) != 32 or any(c not in '0123456789abcdef' for c in cycle_id):
            raise ValueError('closed_cycle_identity_invalid')
        block = self.sources.block
        now = block.now_factory()
        current = self.runtime.load_current_state()
        from packages.application.vitrina_economics import EFFECTIVE_DATE
        enabled = (live.reporting_config(self.runtime.db_path, current.config_v2)[0]
            if live.current_business_date_iso(now) >= EFFECTIVE_DATE
            else sorted((v for v in current.config_v2 if v.enabled), key=lambda v: v.display_order))
        value = self.status()
        if value is None or all(item['state'] == 'acknowledged' for item in value['dates'].values()):
            selected = self._due()
            value = dict(schema_version=1, cycle_id=cycle_id, bundle_version=current.bundle_version,
                collection_bundle_version=current.bundle_version,
                authority=capture_authority(self.runtime.runtime_dir, db_path=self.runtime.db_path),
                dates={day: dict(state='planned', sources={key: dict(anchor=None, before=None,
                    attempt_before=None, outcome='planned') for key in sorted(live._expand_selected_source_keys_for_dependencies(set(keys)))})
                    for day, keys in selected.items()})
            if not selected:
                return value
            self._write(value)  # Demand precedes any source capture.
        if not self._reconcile_publication_context(value, current, enabled):
            return deepcopy(value)
        for day, item in value['dates'].items():
            if item['state'] == 'acknowledged':
                continue
            if day >= (date.fromisoformat(live.current_business_date_iso(now)) - timedelta(days=1)).isoformat():
                raise ClosedBacklogConflict('closed_obligation_date_changed')
            if item.get('deferred_reason'):
                continue
            eligible = []
            was_uncertain = item['state'] == 'capturing'
            for key, source in item['sources'].items():
                nm_ids = [v.nm_id for v in enabled]
                if key == 'stocks':
                    nm_ids = live.require_stock_catalog_scope(self.runtime.db_path)['nm_ids']
                anchor = self._valid_anchor(key, day, nm_ids)
                if source['anchor'] is not None:
                    if anchor != source['anchor']:
                        raise ClosedBacklogConflict('closed_accepted_source_changed:' + key)
                    continue
                state = self.runtime.load_temporal_source_closure_state(source_key=key,
                    target_date=day, slot_kind=live.TEMPORAL_SLOT_YESTERDAY_CLOSED)
                if anchor is not None and source['outcome'] != 'planned':
                    source.update(anchor=anchor, outcome='accepted_retained' if anchor == source['before'] else 'accepted',
                                  latest_attempt=asdict(state) if state else None)
                    continue
                if (was_uncertain or source['outcome'] in {'capturing', 'outcome_unknown'}) and (state is None or asdict(state) == source['attempt_before']):
                    source['outcome'] = 'outcome_unknown'
                    continue
                if live._closure_attempt_is_due(state, now):
                    eligible.append(key)
                else:
                    source['outcome'] = 'backoff_or_exhausted'
            if eligible:
                for key in eligible:
                    source = item['sources'][key]
                    source['before'] = self._anchor(key, day)
                    state = self.runtime.load_temporal_source_closure_state(source_key=key, target_date=day,
                        slot_kind=live.TEMPORAL_SLOT_YESTERDAY_CLOSED)
                    source['attempt_before'] = asdict(state) if state else None
                    nm_ids = live.require_stock_catalog_scope(self.runtime.db_path)['nm_ids'] if key == 'stocks' else [v.nm_id for v in enabled]
                    source['attempt_scope_fingerprint'] = self._attempt_scope(enabled, nm_ids)
                    source['outcome'] = 'capturing'
                item['state'] = 'capturing'; self._write(value)
                # Canonical ONE closed slot; no main build/mature/rollover/sync.
                with capture_build_inputs(self.runtime.db_path, runtime_dir=self.runtime.runtime_dir):
                    block._load_live_sources(enabled, [live.SheetVitrinaV1TemporalSlot(
                        slot_key=live.TEMPORAL_SLOT_YESTERDAY_CLOSED,
                        slot_label=live.TEMPORAL_SLOT_YESTERDAY_CLOSED, column_date=day)],
                        None, execution_mode=live.EXECUTION_MODE_PERSISTED_RETRY, source_keys=set(eligible))
                for key in eligible:
                    nm_ids = live.require_stock_catalog_scope(self.runtime.db_path)['nm_ids'] if key == 'stocks' else [v.nm_id for v in enabled]
                    anchor = self._valid_anchor(key, day, nm_ids)
                    source = item['sources'][key]
                    state = self.runtime.load_temporal_source_closure_state(source_key=key, target_date=day,
                        slot_kind=live.TEMPORAL_SLOT_YESTERDAY_CLOSED)
                    source.update(anchor=anchor, outcome=('accepted_retained' if anchor == source['before'] else 'accepted') if anchor else 'attempt_pending',
                                  latest_attempt=asdict(state) if state else None)
            if item['state'] not in {'ready', 'acknowledged'}:
                item['state'] = 'accepted' if all(v['anchor'] for v in item['sources'].values()) else 'pending'
            self._write(value)
        return deepcopy(value)

    def compose(self, main_handle, day):
        require_cycle_owner(self.runtime)
        value = self.status()
        item = value['dates'].get(day) if value else None
        if item is None or not all(v['anchor'] for v in item['sources'].values()):
            raise ClosedBacklogConflict('closed_sources_not_accepted')
        if item.get('deferred_reason'):
            raise ClosedBacklogConflict(item['deferred_reason'])
        return self.sources._compose_closed(main_handle, day, {key: v['anchor'] for key, v in item['sources'].items()})

    def _defer_publication(self, day, reason):
        """Persist only a known cache refusal, never a success or source retry."""
        require_cycle_owner(self.runtime)
        if not reason.startswith(('closed_operand_unavailable:', 'closed_operand_invalid:')):
            raise ClosedBacklogConflict('closed_deferral_reason_invalid')
        value = self.status()
        if value is None or day not in value['dates'] or value['dates'][day]['state'] == 'acknowledged':
            raise ClosedBacklogConflict('closed_obligation_missing')
        item = value['dates'][day]
        item['deferred_reason'] = reason
        if item['state'] == 'ready':
            item['previous_ready'] = item.pop('ready')
            item['state'] = 'accepted'
        self._write(value)

    def _ready_proof(self, value, day):
        if value['authority'] != capture_authority(self.runtime.runtime_dir, db_path=self.runtime.db_path):
            raise ClosedBacklogConflict('closed_source_authority_changed')
        item = value['dates'][day]
        if item.get('deferred_reason'):
            raise ClosedBacklogConflict(item['deferred_reason'])
        if not item['sources'] or not all(source['anchor'] for source in item['sources'].values()):
            raise ClosedBacklogConflict('closed_sources_not_accepted')
        with readonly(self.runtime.db_path) as conn:
            for key, source in item['sources'].items():
                if self._anchor(key, day, connection=conn) != source['anchor']:
                    raise ClosedBacklogConflict('closed_accepted_source_changed:' + key)
            row = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                (value['bundle_version'], day)).fetchone()
            if row is None:
                raise ClosedBacklogConflict('closed_ready_missing:' + day)
            encoded = row['plan_json']; plan = json.loads(encoded)
            inputs = plan.get('metadata', {}).get('publication_inputs', {})
            for key, source in item['sources'].items():
                if inputs.get('consumed', {}).get(canonical([key, day, source['anchor']['snapshot_role']])) != source['anchor']:
                    raise ClosedBacklogConflict('closed_ready_source_not_consumed:' + key)
            receipts = conn.execute('SELECT operation_id,attempt_id,inputs_json,after_digest FROM sheet_vitrina_v1_ready_publications '
                "WHERE bundle_version=? AND as_of_date=? AND state='complete' AND ready_required=1 AND after_digest=? LIMIT 2",
                (value['bundle_version'], day, digest(encoded))).fetchall()
            if len(receipts) != 1 or json.loads(receipts[0]['inputs_json']).get('build') != inputs:
                raise ClosedBacklogConflict('closed_ready_complete_receipt_missing:' + day)
            receipt = receipts[0]
            return dict(bundle_version=value['bundle_version'], as_of_date=day, snapshot_id=row['snapshot_id'],
                refreshed_at=row['refreshed_at'], after_digest=receipt['after_digest'],
                operation_id=receipt['operation_id'], attempt_id=receipt['attempt_id'])

    def record_ready(self, day):
        require_cycle_owner(self.runtime)
        value = self.status()
        if value is None or day not in value['dates']:
            raise ClosedBacklogConflict('closed_obligation_missing')
        proof = self._ready_proof(value, day)
        value['dates'][day].update(state='ready', ready=proof)
        self._write(value)
        return deepcopy(proof)

    def publication_dates(self):
        """Frozen ready subset; pending obligations never become an ack date."""
        require_cycle_owner(self.runtime)
        value = self.status()
        if value is None:
            return ()
        dates = tuple(sorted(day for day, item in value['dates'].items() if item['state'] == 'ready' and not item.get('deferred_reason')))
        for day in dates:
            if self._ready_proof(value, day) != value['dates'][day].get('ready'):
                raise ClosedBacklogConflict('closed_ready_changed:' + day)
        return dates

    @staticmethod
    def _selected_dates(value, backfill_dates):
        if (type(backfill_dates) is not tuple or len(backfill_dates) > 2
                or any(type(day) is not str for day in backfill_dates)
                or backfill_dates != tuple(sorted(set(backfill_dates)))
                or any(day not in value['dates'] or value['dates'][day]['state'] != 'ready'
                       or value['dates'][day].get('deferred_reason') for day in backfill_dates)):
            raise ClosedBacklogConflict('closed_native_dates_changed')
        return backfill_dates

    def validate_native_readonly(self, *, value, adapter, store, backfill_dates):
        """Fixed child RO seam: existing source→ready→CURRENT predicates only.

        Does not grant ownership, run mount commands or acknowledge a receipt.
        The owned supervisor alone may authenticate a returned child proof.
        """
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        from packages.application.web_vitrina_history_store import HistoryStore
        if (type(adapter) is not LiveNativeAdapter or type(store) is not HistoryStore
                or adapter.db_path != Path(self.runtime.db_path).resolve()
                or adapter.runtime_dir != Path(self.runtime.runtime_dir).resolve()):
            raise ClosedBacklogConflict('closed_native_context_changed')
        self._selected_dates(value, backfill_dates)
        if value != self.status():
            raise ClosedBacklogConflict('closed_receipt_changed')
        vector = adapter.capture()
        fence = adapter.fence
        pointer = store._current()
        edition = store.edition()
        proofs = store.day_proofs(edition)
        for day in backfill_dates:
            item = value['dates'][day]
            ready = self._ready_proof(value, day)
            if ready != item.get('ready'):
                raise ClosedBacklogConflict('closed_ready_changed:' + day)
            with readonly(self.runtime.db_path) as conn:
                # Native exact-date preference and ordering, without a new
                # selector or synthetic edition evidence.
                selected = conn.execute('SELECT bundle_version,snapshot_id,plan_json FROM sheet_vitrina_v1_ready_snapshots '
                    'WHERE as_of_date=? ORDER BY activated_at DESC,refreshed_at DESC,as_of_date DESC,bundle_version DESC LIMIT 1', (day,)).fetchone()
            if not selected or selected['bundle_version'] != ready['bundle_version'] or selected['snapshot_id'] != ready['snapshot_id']:
                raise ClosedBacklogConflict('closed_native_binding_changed:' + day)
            if day not in json.loads(selected['plan_json'])['date_columns']:
                raise ClosedBacklogConflict('closed_native_date_not_bound:' + day)
            if day not in edition['days'] or proofs.get(day) != dict(epoch=vector['epoch'], token=vector['dates'].get(day)):
                raise ClosedBacklogConflict('closed_native_publication_pending:' + day)
        if adapter.capture() != vector or adapter.fence != fence or store._current() != pointer or self.status() != value:
            raise ClosedBacklogConflict('closed_native_changed_during_ack')
        for day in backfill_dates:
            if self._ready_proof(value, day) != value['dates'][day]['ready']:
                raise ClosedBacklogConflict('closed_ready_changed:' + day)
        return dict(receipt_digest=digest(canonical(value)), current=pointer, vector=vector, fence=fence,
            native={day: dict(edition_id=pointer['current'], **proofs[day]) for day in backfill_dates})

    def acknowledge(self, *, adapter, store, history_config, backfill_dates=None):
        """Original in-process path retains ownership and physical admission."""
        require_cycle_owner(self.runtime)
        from apps.web_vitrina_history_candidate_build import runtime_storage_admission
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        from packages.application.web_vitrina_history_store import HistoryStore
        if (type(adapter) is not LiveNativeAdapter or type(store) is not HistoryStore
                or adapter.db_path != Path(self.runtime.db_path).resolve()
                or adapter.runtime_dir != Path(self.runtime.runtime_dir).resolve()
                or adapter.formula_epoch != history_config.formula_epoch
                or store.root != Path(history_config.candidate_root) / 'history'):
            raise ClosedBacklogConflict('closed_native_context_changed')
        runtime_storage_admission(Path(history_config.candidate_root), history_config.runtime_contract, history_config.formula_epoch)
        value = self.status()
        if value is None:
            return None
        dates = tuple(sorted(value['dates'])) if backfill_dates is None else backfill_dates
        checked = self.validate_native_readonly(value=value, adapter=adapter, store=store, backfill_dates=dates)
        for day in dates:
            value['dates'][day].update(state='acknowledged', native=checked['native'][day])
        self._write(value)
        return deepcopy(value)

    def _acknowledge_verified_native(self, proof, *, backfill_dates):
        """Only actual owned supervisor proof can bridge child RO to parent write."""
        require_cycle_owner(self.runtime)
        from packages.application.owned_history_native_ack import consume_closed_history_ack
        value = self.status()
        if value is None or self.publication_dates() != backfill_dates:
            raise ClosedBacklogConflict('closed_native_dates_changed')
        self._selected_dates(value, backfill_dates)
        native = consume_closed_history_ack(proof, self.runtime.runtime_dir, digest(canonical(value)), backfill_dates)
        if self.status() != value:
            raise ClosedBacklogConflict('closed_receipt_changed')
        for day in backfill_dates:
            value['dates'][day].update(state='acknowledged', native=dict(native[day]))
        self._write(value)
        return deepcopy(value)
