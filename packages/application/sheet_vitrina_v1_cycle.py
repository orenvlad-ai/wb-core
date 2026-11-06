"""Dormant same-daemon sequential cycle; receipts are evidence, never a replay queue."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4

from packages.application.ready_publication import canonical, digest, readonly
from packages.application.web_vitrina_snapshot_admission import process_identity, _atomic
from packages.business_time import current_business_date_iso
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog, ClosedBacklogConflict

OPERATION = 'cycle'
HEAVY_OPERATIONS = ('auto_update', 'refresh', 'refresh_group', OPERATION)
STAGES = ('closed_sources', 'api_sources', 'finance_sources', 'fbs_generation', 'warehouse',
          'daily_projection', 'closed_ready', 'final_ready', 'rolling14')
ACTIVE = {'accepted', 'running'}
CAPTURE_UNAVAILABLE_KINDS = frozenset({'missing', 'error', 'empty', 'incomplete', 'not_available',
    'blocked', 'rate_limited', 'not_found', 'closure_pending', 'closure_retrying',
    'closure_rate_limited', 'closure_exhausted'})


class CycleConflict(ValueError):
    pass


class CycleStageFailure(RuntimeError):
    pass


class CycleDateRollover(CycleStageFailure):
    """A completed boundary or a guarded, not-yet-started dated operation."""
    def __init__(self, expected_date, observed_date):
        super().__init__('cycle_business_date_changed')
        self.expected_date = expected_date
        self.observed_date = observed_date


def require_cycle_business_date(expected_date, now):
    observed = current_business_date_iso(now)
    if observed != expected_date:
        raise CycleDateRollover(expected_date, observed)


def validate_recovery_as_of_date(day, now=None):
    """One explicit closed date in rolling14; a receipt is readable after rollover."""
    if not isinstance(day, str):
        raise ValueError('recovery_as_of_date_requires_iso_date')
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError('recovery_as_of_date_requires_iso_date') from exc
    if parsed.isoformat() != day:
        raise ValueError('recovery_as_of_date_requires_iso_date')
    if now is not None:
        today = date.fromisoformat(current_business_date_iso(now))
        if not today - timedelta(days=13) <= parsed < today - timedelta(days=1):
            raise ValueError('recovery_as_of_date_outside_closed_rolling14')
    return day


class FbsGenerationFailure(CycleStageFailure):
    def __init__(self, latest, reason_code='generation_incomplete'):
        super().__init__('fbs_new_complete_generation_missing')
        self._latest = dict(latest) if latest is not None else None
        self._reason_code = reason_code

    def diagnostic(self):
        from packages.application.wb_fbs_warehouse_registry import fbs_generation_failure
        return fbs_generation_failure(self._latest, self._reason_code)


@dataclass(frozen=True)
class CycleHistoryConfig:
    candidate_root: Path
    runtime_contract: Path
    formula_epoch: str
    budget_seconds: float = 180
    max_recomputes: int = 31

    def fingerprint(self):
        if not 0 < self.budget_seconds <= 240 or not 1 <= self.max_recomputes <= 31:
            raise ValueError('cycle_history_budget_invalid')
        return digest(canonical({**asdict(self), 'contract_digest': digest(self.runtime_contract.read_text())}))


@dataclass(frozen=True)
class StageProof:
    versions: dict[str, str]
    warnings: tuple[dict[str, str | bool], ...] = ()


def _warning(source, policy):
    return {key: str(source.get(key, ''))[:256] for key in
        ('source_key', 'temporal_slot', 'date', 'kind', 'latest_attempt_kind', 'accepted_digest')} | {'policy': policy}


def validate_collection(summary, *, require_full_scope=False):
    warnings = []
    if not summary.get('slots') or summary.get('collection_complete') is not True:
        raise CycleStageFailure('collection_proof_missing')
    if require_full_scope and summary.get('full_scope') is not True:
        raise CycleStageFailure('collection_full_scope_required')
    for slot in summary['slots']:
        policy = slot['policy']
        if slot.get('invalid_payload_reason'):
            raise CycleStageFailure('collection_payload_invalid:' + slot['source_key'] + ':' + slot['invalid_payload_reason'])
        if bool(slot['accepted']) != bool(slot['accepted_digest']):
            raise CycleStageFailure('collection_accepted_proof_inconsistent:' + slot['source_key'])
        if slot['accepted'] and slot.get('outcome_digest') != slot['accepted_digest']:
            raise CycleStageFailure('collection_accepted_digest_changed:' + slot['source_key'])
        if policy in {'archive_only', 'temporal_role_unavailable'}:
            warnings.append(_warning(slot, policy))
        elif policy == 'unavailable' and not slot['accepted'] and slot['kind'] in CAPTURE_UNAVAILABLE_KINDS:
            warnings.append(_warning(slot, 'captured_unavailable'))
        elif policy in {'accepted_partial', 'accepted_retained'}:
            if not slot['accepted']:
                raise CycleStageFailure('collection_accepted_proof_missing:' + slot['source_key'])
            warnings.append(_warning(slot, policy))
        elif policy != 'accepted_complete' or not slot['accepted']:
            raise CycleStageFailure('source_policy_unknown:' + slot['source_key'])
    return StageProof({**{key: summary[key] for key in ('scope_fingerprint', 'provenance_fingerprint', 'bundle_version')},
        'source_capture_outcomes': canonical(summary['slots'])}, tuple(warnings))


def _duration_ms(start, end):
    if not start or not end:
        return None
    try:
        return max(0, int((datetime.fromisoformat(end.replace('Z', '+00:00')) -
            datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def cycle_error_diagnostic(exc, receipt):
    """Bounded evidence, never exception bodies, SQL, locals or provider headers.

    Unknown messages are deliberately omitted rather than regex-redacted: a
    response can contain an arbitrary secret without a recognizable key.
    """
    import sqlite3
    import re
    root = Path(__file__).resolve().parents[2]
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        kind = type(current).__name__
        item = {'type': kind[:80] if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', kind) else 'Exception',
            'message_policy': 'raw_message_omitted', 'frames': []}
        from packages.contracts.stocks_failure_diagnostics import stocks_failure_diagnostic
        stocks_failure = stocks_failure_diagnostic(current)
        if stocks_failure is not None:
            item['stocks_failure'] = stocks_failure
        from packages.application.dependency_proof import CompletionProofError
        from packages.application.wb_finance_weekly import FinanceStaleCostHandoffError
        if isinstance(current, (CompletionProofError, FinanceStaleCostHandoffError)):
            item['proof_diagnostic'] = current.diagnostic()
        if isinstance(current, sqlite3.Error):
            code = getattr(current, 'sqlite_errorcode', None)
            if type(code) is int:
                item['sqlite_errorcode'] = code
            name = getattr(current, 'sqlite_errorname', '')
            if isinstance(name, str) and re.fullmatch(r'SQLITE_[A-Z_0-9]{1,60}', name):
                item['sqlite_errorname'] = name
            # Exact known messages contain no SQL identifiers or user data.
            message = str(current)
            if message in {'database is locked', 'database table is locked',
                    'database is busy', 'unable to open database file',
                    'attempt to write a readonly database', 'disk I/O error',
                    'database or disk is full', 'cannot start a transaction within a transaction',
                    'database disk image is malformed', 'interrupted'}:
                item.update(message=message, message_policy='known_sqlite_literal')
        status = getattr(current, 'status_code', None)
        if type(status) is int and 100 <= status <= 599:
            item['http_status'] = status
        # urllib HTTPError uses code; no URL, headers or response body is read.
        from urllib.error import HTTPError
        if isinstance(current, HTTPError) and type(current.code) is int and 100 <= current.code <= 599:
            item['http_status'] = current.code
        tb = current.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            try:
                relative = Path(code.co_filename).resolve().relative_to(root)
            except ValueError:
                relative = None
            if relative is not None and relative.parts[0] in {'apps', 'packages'}:
                item['frames'].append({'file': relative.as_posix(),
                    'function': code.co_name[:100], 'line': tb.tb_lineno})
            tb = tb.tb_next
        item['frames'] = item['frames'][-8:]
        chain.append(item)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {'schema_version': 1, 'stage': receipt.get('current_stage') or 'initialization',
        'observed_at': receipt.get('finished_at'), 'exceptions': chain,
        'effects': 'not_inferred_from_exception'}


def cycle_summary(receipt):
    """Report execution, confirmed publication and quality independently.

    No domain reads, retries, recovery decisions or changed completion rules.
    """
    stages = receipt.get('stages', [])
    ready = next((s for s in stages if s['stage'] == 'final_ready'), {})
    versions = ready.get('versions', {})
    if ready.get('status') in {'complete', 'degraded'}:
        publication = 'confirmed'
    elif ready.get('started_at'):
        publication = 'unknown'
    else:
        publication = 'not_started'
    quality = ('degraded' if ready.get('status') == 'degraded' else
        'complete' if ready.get('status') == 'complete' else 'unknown')
    return {'schema_version': 1, 'cycle_id': receipt['cycle_id'],
        **({'rollover': dict(receipt['rollover'])} if receipt.get('rollover') else {}),
        'execution': receipt['status'], 'failed_stage': receipt.get('current_stage') or
            ('initialization' if receipt['status'] in {'failed', 'interrupted'} else ''),
        'started_at': receipt.get('started_at'), 'finished_at': receipt.get('finished_at'),
        'duration_ms': _duration_ms(receipt.get('started_at'), receipt.get('finished_at')),
        'publication': {'scope': 'final_ready', 'status': publication, 'quality': quality,
            **{k: versions[k] for k in ('snapshot_id', 'refreshed_at', 'publication_operation_id',
                'publication_attempt_id', 'ready_semantic_status') if k in versions}},
        'stages': [{'stage': s['stage'], 'status': s['status'],
            'duration_ms': _duration_ms(s.get('started_at'), s.get('finished_at')),
            'warning_count': len(s.get('warnings', []))} for s in stages],
        'details': receipt['cycle_id'] + '.json'}


def _observe_terminal(receipt, exc=None):
    # Observability must never replace the original error or fail a good cycle.
    try:
        if exc is not None:
            receipt['error_diagnostic'] = cycle_error_diagnostic(exc, receipt)
    except Exception:
        receipt['diagnostic_status'] = 'unavailable'
    try:
        receipt['summary'] = cycle_summary(receipt)
    except Exception:
        receipt['summary_status'] = 'unavailable'


class CycleReceiptStore:
    """Small private JSONs and one fixed single-flight; construction is read-only."""
    def __init__(self, runtime_dir, timestamp_factory):
        self.runtime_dir = Path(runtime_dir)
        self.root = self.runtime_dir / 'sheet-vitrina-cycles'
        self.timestamp_factory = timestamp_factory

    def read(self, cycle_id):
        if len(cycle_id) != 32 or any(c not in '0123456789abcdef' for c in cycle_id):
            raise ValueError('invalid_cycle_identity')
        path = self.root / (cycle_id + '.json')
        if not path.exists():
            return None
        with path.open() as handle:
            stat = os.fstat(handle.fileno())
            if stat.st_size > 256 * 1024:
                raise CycleConflict('cycle_receipt_size')
            value = json.load(handle)
        file_identity = [stat.st_dev, stat.st_ino, stat.st_mtime_ns]
        evidence_path = self.root / (cycle_id + '.evidence')
        # A separate bounded evidence file preserves the original receipt and
        # its proof bytes at the existing size limit. Bind it to exact content:
        # a crash between the two renames must never attach a stale terminal.
        try:
            if evidence_path.stat().st_size <= 32768:
                evidence = json.loads(evidence_path.read_text())
                if (evidence.get('receipt_digest') == digest(canonical(value))
                        and evidence.get('receipt_file_identity') == file_identity):
                    value.update(evidence['fields'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if value['status'] in ACTIVE and process_identity(value['owner_pid']) != value['process_identity']:
            value.update(status='interrupted', error_code='cycle_owner_lost')
            for item in value['stages']:
                if item['status'] == 'running':
                    item.update(status='interrupted', error_code='outcome_uncertain')
            _observe_terminal(value)  # Read-side projection only; never writes/replays.
        return value

    def write(self, receipt):
        raw = canonical(receipt)
        evidence = None
        if len(raw.encode()) > 256 * 1024 and ('summary' in receipt or 'error_diagnostic' in receipt):
            fields = {k: receipt[k] for k in ('summary', 'error_diagnostic') if k in receipt}
            receipt = {k: v for k, v in receipt.items() if k not in {'summary', 'error_diagnostic'}}
            raw = canonical(receipt)  # Preserve the pre-existing receipt budget/behavior.
            fields['diagnostic_storage'] = 'bounded_sidecar'
            evidence = {'receipt_digest': digest(canonical(receipt)), 'fields': fields}
            if len(canonical(evidence).encode()) > 32768:
                fields.pop('error_diagnostic', None)
                fields.update(diagnostic_truncated=True, diagnostic_omitted_count=1)
        if len(raw.encode()) > 256 * 1024:
            raise CycleConflict('cycle_receipt_size')
        _atomic(self.root / (receipt['cycle_id'] + '.json'), receipt)
        if evidence is not None:
            try:
                stat = (self.root / (receipt['cycle_id'] + '.json')).stat()
                evidence['receipt_file_identity'] = [stat.st_dev, stat.st_ino, stat.st_mtime_ns]
                if len(canonical(evidence).encode()) <= 32768:
                    _atomic(self.root / (receipt['cycle_id'] + '.evidence'), evidence)
            except OSError:
                pass  # Auxiliary storage may not mask the domain outcome.
        # _atomic fsyncs the file; directory fsync makes the rename durable too.
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def roll_over(self, receipt, *, observed_date):
        """Archive one finished epoch before starting fresh, under the same lease.

        This is not a recovery queue: a lost owner still interrupts the durable
        request. Source handles and incomplete operations are never resumed.
        """
        if receipt.get('rollover'):
            raise CycleStageFailure('cycle_rollover_limit')
        if date.fromisoformat(observed_date) != date.fromisoformat(receipt['business_date']) + timedelta(days=1):
            raise CycleStageFailure('cycle_rollover_date_invalid')
        archive = {key: value for key, value in receipt.items()
                   if key not in {'summary', 'error_diagnostic'}}
        archive['epoch_disposition'] = 'rolled_over'
        archive['epoch_ended_at'] = receipt['epoch_transition']['observed_at']
        raw = canonical(archive)
        if len(raw.encode()) > 256 * 1024:
            raise CycleConflict('cycle_receipt_size')
        name = receipt['cycle_id'] + '.rollover-0.json'
        path = self.root / name
        if path.exists():
            raise CycleConflict('cycle_rollover_archive_exists')
        _atomic(path, archive)
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        rollover = dict(from_date=receipt['business_date'], to_date=observed_date,
                        archive_file=name, archive_digest=digest(path.read_text()),
                        stage=receipt['current_stage'], observed_at=receipt['epoch_transition']['observed_at'])
        receipt.setdefault('accepted_business_date', receipt['business_date'])
        receipt.update(business_date=observed_date, rollover=rollover, status='running', current_stage='',
                       finished_at=None, error_code='', final_versions={},
                       stages=[dict(stage=name, status='pending', versions={}, warnings=[]) for name in STAGES])
        for key in ('summary', 'error_diagnostic', 'history_failure', 'fbs_failure', 'epoch_transition'):
            receipt.pop(key, None)
        self.write(receipt)

    def _request_identity(self, *, request_key, slot_utc, config, recovery_as_of_date=None):
        if not request_key or len(request_key) > 160:
            raise ValueError('invalid_cycle_request')
        slot = datetime.fromisoformat(slot_utc.replace('Z', '+00:00'))
        if slot.tzinfo is None or slot.utcoffset() != timedelta(0):
            raise ValueError('cycle_slot_requires_utc')
        slot_utc = slot.astimezone(timezone.utc).isoformat()
        manual = request_key.startswith('manual-cycle:')
        scope = {'slot': slot_utc, 'history': config.fingerprint(),
                 'scope': 'full_manual_daily' if manual else 'full_auto_daily'}
        if recovery_as_of_date is not None:
            if not manual:
                raise ValueError('recovery_requires_manual_cycle')
            scope['recovery_as_of_date'] = validate_recovery_as_of_date(recovery_as_of_date)
        fingerprint = digest(canonical(scope))
        cycle_id = digest(request_key).removeprefix('sha256:')[:32]
        # Explicit manual operations have their own durable identity. They never
        # overwrite or consume the scheduled slot's receipt/index.
        slot_path = self.root / (('manual-' + cycle_id if manual else
                                 'slot-' + digest(slot_utc).removeprefix('sha256:')[:32]) + '.json')
        return slot_utc, fingerprint, cycle_id, slot_path

    def matching(self, *, request_key, slot_utc, config, recovery_as_of_date=None):
        """Read-only dedup with the same scope checks; never grants admission."""
        _, fingerprint, cycle_id, slot_path = self._request_identity(
            request_key=request_key, slot_utc=slot_utc, config=config, recovery_as_of_date=recovery_as_of_date)
        prior = self.read(cycle_id)
        if prior is None and slot_path.exists():
            prior = self.read(json.loads(slot_path.read_text())['cycle_id'])
        if prior is not None and prior['request_fingerprint'] != fingerprint:
            raise CycleConflict('cycle_request_conflict')
        return prior

    def accept(self, *, request_key, slot_utc, config, now, dispatch_id=None, recovery_as_of_date=None):
        slot_utc, fingerprint, cycle_id, slot_path = self._request_identity(
            request_key=request_key, slot_utc=slot_utc, config=config, recovery_as_of_date=recovery_as_of_date)
        def same_request_or_slot():
            return self.matching(request_key=request_key, slot_utc=slot_utc, config=config, recovery_as_of_date=recovery_as_of_date)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stream = (self.runtime_dir / '.sheet-vitrina-cycle.lock').open('a+b')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            prior = same_request_or_slot()
            if prior:
                if prior['request_fingerprint'] != fingerprint:
                    raise CycleConflict('cycle_request_conflict')
                return prior, None
            raise CycleConflict('cycle_busy')
        try:
            prior = same_request_or_slot()
            if prior:
                if prior['request_fingerprint'] != fingerprint:
                    raise CycleConflict('cycle_request_conflict')
                if prior['status'] in ACTIVE:
                    # Lock ownership ended: no in-memory source handle is recoverable.
                    prior.update(status='interrupted', error_code='cycle_owner_lost', finished_at=self.timestamp_factory())
                    for item in prior['stages']:
                        if item['status'] == 'running':
                            item.update(status='interrupted', error_code='outcome_uncertain')
                    self.write(prior)
                stream.close()
                return prior, None
            if recovery_as_of_date is not None:
                validate_recovery_as_of_date(recovery_as_of_date, now)
            latest = self.root / 'latest.json'
            if latest.exists():
                old = self.read(json.loads(latest.read_text())['cycle_id'])
                if old and old['status'] in ACTIVE:
                    old.update(status='interrupted', error_code='cycle_owner_lost', finished_at=self.timestamp_factory())
                    for item in old['stages']:
                        if item['status'] == 'running':
                            item.update(status='interrupted', error_code='outcome_uncertain')
                    self.write(old)
            identity = process_identity(os.getpid())
            if not identity:
                raise CycleConflict('cycle_requires_process_identity')
            receipt = dict(schema_version=1, cycle_id=cycle_id, request_key=request_key,
                request_fingerprint=fingerprint, slot_utc=slot_utc,
                history_config_fingerprint=config.fingerprint(),
                business_date=current_business_date_iso(now), owner_pid=os.getpid(), process_identity=identity,
                job_id='', status='accepted', current_stage='', started_at=self.timestamp_factory(),
                finished_at=None, stages=[dict(stage=name, status='pending', versions={}, warnings=[]) for name in STAGES],
                final_versions={}, error_code='')
            if recovery_as_of_date is not None:
                receipt['recovery_as_of_date'] = recovery_as_of_date
            if dispatch_id is not None:
                receipt['dispatch_id'] = dispatch_id
            receipt['trigger_kind'] = 'manual' if request_key.startswith('manual-cycle:') else 'scheduled'
            self.write(receipt)
            _atomic(slot_path, {'cycle_id': cycle_id})
            _atomic(latest, {'cycle_id': cycle_id})
            self.write(receipt)  # fsync directory after all fixed index renames.
            return receipt, stream
        except BaseException:
            stream.close()
            raise


def finance_raw_proof(entrypoint):
    daily = entrypoint.wb_finance_daily_block
    with closing(daily._connect_daily_read()) as conn:
        conn.execute('BEGIN')
        table = daily._raw_pointer_table(conn)
        rows = [dict(row) for row in conn.execute(f'SELECT report_day,batch_id,content_hash,row_count FROM {table} WHERE seller_id=? ORDER BY report_day DESC LIMIT 14', (daily.seller_id,))]
        conn.rollback()
    weekly = entrypoint.wb_finance_weekly_block
    with readonly(entrypoint.runtime.db_path) as conn:
        weeks = [list(row) for row in conn.execute('SELECT week_start,week_end,status,content_hash,raw_row_count FROM wb_finance_weekly_sync WHERE seller_id=? ORDER BY week_start DESC LIMIT 10', (weekly.seller_id,))]
    return {'daily_raw': digest(canonical(rows)), 'weekly_raw': digest(canonical(weeks)),
        # Bounded identities only, never provider rows or derived business values.
        'daily_batches': canonical([{key: row[key] for key in ('report_day', 'batch_id', 'content_hash')} for row in rows]),
        'weekly_sources': canonical([{'week_start': row[0], 'week_end': row[1], 'content_hash': row[3]} for row in weeks])}


def daily_report_proof(block, *, attempts=(), payload=None):
    """Preserve latest statuses; retained operands need exact complete dated raw.

    Visible projection admission stays owned by build_daily_payload. Only its
    admitted rows expose nonempty metrics and positive raw_row_count. Attempts
    outside visible14 still bind their accepted raw batch (canonical backlog).
    """
    payload = payload if payload is not None else block.build_daily_payload()
    visible = {item['day']: item for item in payload['days']}
    latest = {item['report_day']: item for item in attempts}
    days = sorted(set(visible) | set(latest))
    proofs, warnings = [], []
    with closing(block._connect_daily_read()) as conn:
        conn.execute('BEGIN')
        pointers = block._raw_pointer_table(conn)
        batches = pointers.removesuffix('wb_finance_daily_pointers') + 'wb_finance_daily_batches'
        rows = {row['report_day']: dict(row) for row in conn.execute(f'''
            SELECT p.report_day,p.batch_id,p.content_hash,p.row_count,
                   b.batch_id AS accepted_batch,b.content_hash AS accepted_hash,
                   b.row_count AS accepted_count,b.terminal_status,
                   s.batch_id AS sync_batch,s.content_hash AS sync_hash,
                   s.status AS projection_status,s.last_synced_at,
                   a.batch_id AS aggregate_batch,a.content_hash AS aggregate_hash,
                   a.raw_row_count AS aggregate_count,a.metrics_json
            FROM {pointers} p LEFT JOIN {batches} b
              ON b.batch_id=p.batch_id AND b.seller_id=p.seller_id AND b.report_day=p.report_day
            LEFT JOIN wb_finance_daily_sync s ON s.seller_id=p.seller_id AND s.report_day=p.report_day
            LEFT JOIN wb_finance_daily_aggregates a ON a.seller_id=p.seller_id AND a.report_day=p.report_day
            WHERE p.seller_id=? AND p.report_day IN ({','.join('?' for _ in days)})''',
            (block.seller_id, *days))} if days else {}
        conn.rollback()
    for day in days:
        item, attempt, raw = visible.get(day), latest.get(day), rows.get(day)
        status = str((attempt or item)['status'])
        accepted = bool(raw and raw['batch_id'] == raw['accepted_batch']
            and raw['content_hash'] == raw['accepted_hash'] and raw['row_count'] == raw['accepted_count']
            and raw['row_count'] > 0 and raw['terminal_status'] == 204)
        if item is not None:
            # Bind the already admitted readback to this exact raw/projection
            # version; equal row counts cannot hide a race advancing the batch.
            accepted = bool(accepted and item['raw_row_count'] == raw['row_count'] and item['metrics']
                and raw['sync_batch'] == raw['aggregate_batch'] == raw['batch_id']
                and raw['sync_hash'] == raw['aggregate_hash'] == raw['content_hash']
                and raw['aggregate_count'] == raw['row_count']
                and raw['projection_status'] == item['status'] and raw['last_synced_at'] == item['last_synced_at']
                and raw['metrics_json'] and json.loads(raw['metrics_json']) == item['metrics'])
        if raw and not accepted:
            raise CycleStageFailure('daily_accepted_operand_invalid:' + day)
        if status in {'completed', 'loaded_preliminary'}:
            if not accepted or (attempt and attempt.get('batch_id') != raw['batch_id']):
                raise CycleStageFailure('daily_accepted_operand_missing:' + day)
            policy = 'accepted_complete' if status == 'completed' else 'accepted_provisional'
        elif status in {'error_loading', 'rate_limited'}:
            if not accepted:
                raise CycleStageFailure('daily_failed_operand_unavailable:' + day + ':' + status)
            policy = 'accepted_retained_after_failed_attempt'
        elif status == 'waiting':
            policy = 'accepted_retained_after_waiting' if accepted else 'official_waiting'
        else:
            raise CycleStageFailure('daily_projection_unavailable:' + day + ':' + status)
        proof = dict(date=day, status=status, projection_status=item['status'] if item else 'outside_visible14',
            attempted=attempt is not None, policy=policy, accepted=bool(accepted),
            batch_id=raw['batch_id'] if accepted else '', content_hash=raw['content_hash'] if accepted else '')
        proofs.append(proof)
        if policy != 'accepted_complete':
            warnings.append({'source_key': 'canonical_finance_daily', **proof})
    return StageProof({'daily_report_proofs': canonical(proofs)}, tuple(warnings))


def run_cycle(entrypoint, store, receipt, history_config, log):
    """At most one fresh next-day epoch; never retry an unknown stage outcome."""
    while True:
        try:
            return _run_cycle_epoch(entrypoint, store, receipt, history_config, log)
        except CycleDateRollover as exc:
            try:
                if exc.expected_date != receipt['business_date']:
                    raise CycleStageFailure('cycle_rollover_context_changed')
                store.roll_over(receipt, observed_date=exc.observed_date)
            except BaseException as failure:
                receipt.update(status='failed' if isinstance(failure, Exception) else 'interrupted', error_code=(str(failure) if isinstance(failure, CycleStageFailure)
                    else type(failure).__name__)[:256], finished_at=store.timestamp_factory())
                receipt.pop('epoch_transition', None)
                for item in receipt['stages']:
                    if item['status'] == 'running':
                        item.update(status=receipt['status'], error_code=receipt['error_code'],
                                    finished_at=receipt['finished_at'])
                try:
                    _observe_terminal(receipt, failure)
                    store.write(receipt)
                except Exception:
                    pass  # Never replace cancellation/the original transition failure.
                raise


def _run_cycle_epoch(entrypoint, store, receipt, history_config, log):
    """Only the owning admitted worker calls this; no external-stage retries."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    if require_heavy_owner(entrypoint.runtime.runtime_dir).operation != 'cycle':
        raise RuntimeError('owned cycle heavy admission is required')
    recovery_day = receipt.get('recovery_as_of_date')
    source_adapter = None
    closed = None
    backfill_dates = ()
    handle = None
    summary = None
    ready = None
    finance = None
    fbs = None
    def closed_sources():
        if recovery_day is not None:
            return StageProof({'closed_backlog_policy': 'preserved_for_regular_cycle'})
        value = closed.collect(receipt['cycle_id'], include_retained=False)
        warnings = []
        for day, item in value['dates'].items():
            if item.get('deferred_reason'):
                warnings.append(dict(source_key='closed_sources', date=day, policy=item['deferred_reason']))
            for key, source in item['sources'].items():
                if source['outcome'] in {'capturing', 'outcome_unknown'}:
                    raise CycleStageFailure('closed_source_outcome_uncertain:' + key + ':' + day)
                if not source['anchor']:
                    warnings.append(dict(source_key=key, date=day, policy=source['outcome']))
                elif source['outcome'] == 'accepted_retained':
                    warnings.append(dict(source_key=key, date=day, policy='accepted_retained'))
        return StageProof({'closed_receipt_digest': digest(canonical(closed.status())),
            'closed_selected_dates': canonical(tuple(sorted(value['dates'])))}, tuple(warnings))
    def sources():
        nonlocal handle, summary
        handle = source_adapter.collect_sources(as_of_date=recovery_day or entrypoint._cycle_as_of_date(),
            log=log, execution_mode='manual_operator' if recovery_day else 'auto_daily')
        # The signed dated manual request changes only the closed date/retry
        # policy. Every source group and both temporal slots remain mandatory.
        summary = source_adapter.collected_source_summary(handle, **(
            {'allow_manual_full_scope': True} if recovery_day else {}))
        if summary['business_date'] not in {receipt['business_date'], current_business_date_iso(entrypoint.now_factory())}:
            raise CycleStageFailure('cycle_business_date_changed')
        return validate_collection(summary, require_full_scope=True)
    def finance_sources():
        nonlocal finance
        proof = entrypoint._cycle_finance_sources()
        finance = proof.versions
        return proof
    def fbs_generation():
        nonlocal fbs
        proof = entrypoint._cycle_fbs_generation()
        fbs = proof.versions
        return proof
    def final_ready():
        nonlocal ready
        entrypoint._cycle_validate_predecessors(finance, fbs, receipt['final_versions'])
        plan = source_adapter.derive_collected(handle)
        require_cycle_business_date(receipt['business_date'], entrypoint.now_factory())
        ready, proof = entrypoint._cycle_publish_ready(plan, **(
            {'execution_mode': 'manual_operator'} if recovery_day else {}))
        return proof
    def closed_ready():
        nonlocal backfill_dates
        accounting_unavailable = receipt['final_versions'].get('fbs_accounting_status') == 'unavailable'
        if recovery_day is not None:
            return StageProof({'closed_backlog_policy': 'preserved_for_regular_cycle'})
        warnings, proofs = list(closed.prepare_retained(receipt['cycle_id'])), []
        value = closed.status()
        for day, item in (value['dates'].items() if value else ()):
            if item['state'] == 'acknowledged' or item.get('deferred_reason') or not all(source['anchor'] for source in item['sources'].values()):
                continue
            if accounting_unavailable and not closed.retained_replay(day):
                warnings.append(dict(source_key='closed_ready', date=day,
                    policy='deferred_accounting_unavailable'))
                continue
            require_cycle_business_date(receipt['business_date'], entrypoint.now_factory())
            entrypoint._cycle_validate_predecessors(finance, fbs, receipt['final_versions'])
            try:
                # A prior ready commit needs evidence readback, not an accepted
                # source refetch or another dated publication merely for dates.
                try:
                    proof = closed.record_ready(day)
                except ClosedBacklogConflict as exc:
                    if not str(exc).startswith('closed_ready_missing:') and not closed.can_replace_retained_ready(day, exc):
                        raise
                    plan = source_adapter.derive_collected(closed.compose(handle, day))
                    if closed.retained_replay(day):
                        plan = closed.merge_retained_ready(day, plan)
                        _, result = entrypoint._cycle_publish_dated_ready(plan, retained_only=True,
                            expected_business_date=receipt['business_date'])
                    else:
                        _, result = entrypoint._cycle_publish_dated_ready(plan,
                            expected_business_date=receipt['business_date'])
                    warnings.extend(result.warnings)
                    proof = closed.record_ready(day)
                proofs.append(proof)
            except ClosedBacklogConflict as exc:
                if not str(exc).startswith(('closed_operand_unavailable:', 'closed_operand_invalid:')):
                    raise
                closed._defer_publication(day, str(exc))
                warnings.append(dict(source_key='closed_ready', date=day, policy=str(exc)))
        # Previously committed Ready dates retain their existing native proof
        # obligation; unavailable accounting only prevents a new full replay.
        backfill_dates = closed.publication_dates()
        return StageProof({'closed_ready_proofs': canonical(proofs),
            'closed_backfill_dates': canonical(backfill_dates),
            'closed_receipt_digest': digest(canonical(closed.status()))}, tuple(warnings))
    def history():
        if history_config.fingerprint() != receipt['history_config_fingerprint']:
            raise CycleStageFailure('cycle_history_contract_changed')
        from packages.application.fbs_accounting_historical_history import pending_receipt
        historical = (None if receipt['final_versions'].get('fbs_accounting_status') == 'unavailable'
                      else pending_receipt(entrypoint.runtime))
        kwargs=dict(backfill_dates=backfill_dates,closed_receipt=closed if backfill_dates else None)
        if historical is not None:
            historical.freeze_scope(entrypoint.now_factory())
            from datetime import datetime,timedelta
            yesterday=(datetime.fromisoformat(receipt['business_date'])-timedelta(days=1)).date().isoformat()
            kwargs.update(historical_receipt=historical,backfill_dates=tuple(sorted(set(backfill_dates)|{d for d in historical.publication_dates() if d<yesterday})))
        proof = entrypoint._cycle_history(history_config, receipt, ready, **kwargs)
        if backfill_dates:
            value = closed.status()
            if not value or any(value['dates'][day]['state'] != 'acknowledged' for day in backfill_dates):
                raise CycleStageFailure('closed_native_ack_missing')
        # The completed native proof is persisted by the outer stage before
        # rollover. New-day predecessor admission belongs to the fresh epoch.
        if current_business_date_iso(entrypoint.now_factory()) == receipt['business_date']:
            try:
                entrypoint._cycle_validate_predecessors(finance, fbs, receipt['final_versions'])
            except CycleDateRollover as exc:
                if exc.expected_date != receipt['business_date']:
                    raise
        return proof
    try:
        # A durable accepted cycle must terminalize even if constructing its
        # adapters fails before the first source action. Ownership above still
        # precedes every receipt write; no unowned caller can terminalize it.
        if recovery_day is not None:
            if receipt.get('trigger_kind') != 'manual':
                raise CycleStageFailure('recovery_requires_manual_cycle')
            validate_recovery_as_of_date(recovery_day, entrypoint.now_factory())
        source_adapter = entrypoint._cycle_sources()
        if recovery_day is None:
            closed = ClosedBacklog(source_adapter)
        actions = (closed_sources, sources, finance_sources, fbs_generation,
            lambda: entrypoint._cycle_warehouse(store, receipt, fbs),
            entrypoint._cycle_daily_projection, closed_ready, final_ready, history)
        for item, action in zip(receipt['stages'], actions, strict=True):
            require_cycle_business_date(receipt['business_date'], entrypoint.now_factory())
            receipt.update(status='running', current_stage=item['stage'])
            item.update(status='running', started_at=store.timestamp_factory())
            store.write(receipt)  # Before effects: a crash is uncertain, never pending replay.
            from packages.application.fbs_accounting_degraded import degraded_accounting_scope
            with degraded_accounting_scope(entrypoint.runtime, receipt['final_versions']):
                proof = action()
            if not isinstance(proof, StageProof) or not proof.versions or not all(isinstance(v,str) and v for v in proof.versions.values()):
                raise CycleStageFailure('cycle_stage_proof_missing:' + item['stage'])
            item.update(status='degraded' if proof.warnings else 'complete', finished_at=store.timestamp_factory(),
                versions=proof.versions, warnings=list(proof.warnings))
            receipt['final_versions'].update(proof.versions)
            store.write(receipt)
            # A new epoch is authorized only by an already validated, durable
            # completed proof, never by the clock masking a bad/unknown result.
            require_cycle_business_date(receipt['business_date'], entrypoint.now_factory())
        receipt.update(status='degraded' if any(i['status']=='degraded' for i in receipt['stages']) else 'complete',
            finished_at=store.timestamp_factory(), current_stage='')
        _observe_terminal(receipt)
        store.write(receipt)
        return receipt
    except CycleDateRollover as exc:
        # Keep the canonical request running. A transient terminal status can
        # itself terminalize the transport/observer before the new epoch starts.
        receipt.update(status='running', finished_at=None,
            epoch_transition={'from_date': exc.expected_date, 'to_date': exc.observed_date,
                              'observed_at': store.timestamp_factory(),
                              'boundary': receipt.get('current_stage') or 'initialization'})
        store.write(receipt)
        raise
    except BaseException as exc:
        # Small codes only: exception text can contain a provider response/secret.
        from packages.application.owned_history_worker_capability import HistoryDelegationError
        history_failure = exc.diagnostic() if isinstance(exc, HistoryDelegationError) else None
        fbs_failure = exc.diagnostic() if isinstance(exc, FbsGenerationFailure) else None
        code = history_failure["error_code"] if history_failure else str(exc) if isinstance(exc, CycleStageFailure) else type(exc).__name__
        if history_failure:
            receipt['history_failure'] = history_failure
        if fbs_failure:
            receipt['fbs_failure'] = fbs_failure
        receipt.update(status='failed' if isinstance(exc, Exception) else 'interrupted',
            error_code=code[:256], finished_at=store.timestamp_factory())
        for item in receipt['stages']:
            if item['status'] == 'running':
                item.update(status=receipt['status'], error_code=code[:256], finished_at=store.timestamp_factory())
                if history_failure:
                    item['history_failure'] = dict(history_failure)
                if fbs_failure:
                    item['fbs_failure'] = dict(fbs_failure)
        _observe_terminal(receipt, exc)
        store.write(receipt)
        raise
