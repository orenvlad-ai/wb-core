"""Dormant same-daemon sequential cycle; receipts are evidence, never a replay queue."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4

from packages.application.ready_publication import canonical, digest, readonly
from packages.application.web_vitrina_snapshot_admission import process_identity, _atomic
from packages.business_time import current_business_date_iso

OPERATION = 'cycle'
HEAVY_OPERATIONS = ('auto_update', 'refresh', 'refresh_group', OPERATION)
STAGES = ('api_sources', 'finance_sources', 'fbs_generation', 'warehouse',
          'daily_projection', 'final_ready', 'rolling14')
ACTIVE = {'accepted', 'running'}


class CycleConflict(ValueError):
    pass


class CycleStageFailure(RuntimeError):
    pass


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


def validate_collection(summary):
    warnings = []
    if not summary.get('slots'):
        raise CycleStageFailure('collection_proof_missing')
    for slot in summary['slots']:
        policy = slot['policy']
        if policy in {'archive_only', 'temporal_role_unavailable'}:
            warnings.append(_warning(slot, policy))
        elif not slot['accepted'] or not slot['accepted_digest']:
            raise CycleStageFailure('source_not_admitted:' + slot['source_key'])
        elif policy in {'accepted_partial', 'accepted_retained'}:
            warnings.append(_warning(slot, policy))
        elif policy != 'accepted_complete':
            raise CycleStageFailure('source_policy_unknown:' + slot['source_key'])
    return StageProof({key: summary[key] for key in ('scope_fingerprint', 'provenance_fingerprint', 'bundle_version')}, tuple(warnings))


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
        if path.stat().st_size > 256 * 1024:
            raise CycleConflict('cycle_receipt_size')
        value = json.loads(path.read_text())
        if value['status'] in ACTIVE and process_identity(value['owner_pid']) != value['process_identity']:
            value.update(status='interrupted', error_code='cycle_owner_lost')
            for item in value['stages']:
                if item['status'] == 'running':
                    item.update(status='interrupted', error_code='outcome_uncertain')
        return value

    def write(self, receipt):
        raw = canonical(receipt)
        if len(raw.encode()) > 256 * 1024:
            raise CycleConflict('cycle_receipt_size')
        _atomic(self.root / (receipt['cycle_id'] + '.json'), receipt)
        # _atomic fsyncs the file; directory fsync makes the rename durable too.
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _request_identity(self, *, request_key, slot_utc, config):
        if not request_key or len(request_key) > 160:
            raise ValueError('invalid_cycle_request')
        slot = datetime.fromisoformat(slot_utc.replace('Z', '+00:00'))
        if slot.tzinfo is None or slot.utcoffset() != timedelta(0):
            raise ValueError('cycle_slot_requires_utc')
        slot_utc = slot.astimezone(timezone.utc).isoformat()
        fingerprint = digest(canonical({'slot': slot_utc, 'history': config.fingerprint(), 'scope': 'full_auto_daily'}))
        cycle_id = digest(request_key).removeprefix('sha256:')[:32]
        slot_path = self.root / ('slot-' + digest(slot_utc).removeprefix('sha256:')[:32] + '.json')
        return slot_utc, fingerprint, cycle_id, slot_path

    def matching(self, *, request_key, slot_utc, config):
        """Read-only dedup with the same scope checks; never grants admission."""
        _, fingerprint, cycle_id, slot_path = self._request_identity(
            request_key=request_key, slot_utc=slot_utc, config=config)
        prior = self.read(cycle_id)
        if prior is None and slot_path.exists():
            prior = self.read(json.loads(slot_path.read_text())['cycle_id'])
        if prior is not None and prior['request_fingerprint'] != fingerprint:
            raise CycleConflict('cycle_request_conflict')
        return prior

    def accept(self, *, request_key, slot_utc, config, now):
        slot_utc, fingerprint, cycle_id, slot_path = self._request_identity(
            request_key=request_key, slot_utc=slot_utc, config=config)
        def same_request_or_slot():
            return self.matching(request_key=request_key, slot_utc=slot_utc, config=config)
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
    """Only the owning admitted worker calls this; no external-stage retries."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    if require_heavy_owner(entrypoint.runtime.runtime_dir).operation != 'cycle':
        raise RuntimeError('owned cycle heavy admission is required')
    source_adapter = entrypoint._cycle_sources()
    handle = None
    summary = None
    ready = None
    finance = None
    fbs = None
    def sources():
        nonlocal handle, summary
        handle = source_adapter.collect_sources(as_of_date=entrypoint._cycle_as_of_date(),
            log=log, execution_mode='auto_daily')  # Full ordinary scope: no selectors.
        summary = source_adapter.collected_source_summary(handle)
        if summary['business_date'] != receipt['business_date']:
            raise CycleStageFailure('cycle_business_date_changed')
        return validate_collection(summary)
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
        ready, proof = entrypoint._cycle_publish_ready(plan)
        return proof
    def history():
        if history_config.fingerprint() != receipt['history_config_fingerprint']:
            raise CycleStageFailure('cycle_history_contract_changed')
        proof = entrypoint._cycle_history(history_config, receipt, ready)
        entrypoint._cycle_validate_predecessors(finance, fbs, receipt['final_versions'])
        return proof
    actions = (sources, finance_sources, fbs_generation,
        lambda: entrypoint._cycle_warehouse(store, receipt, fbs),
        entrypoint._cycle_daily_projection, final_ready, history)
    try:
        for item, action in zip(receipt['stages'], actions, strict=True):
            if current_business_date_iso(entrypoint.now_factory()) != receipt['business_date']:
                raise CycleStageFailure('cycle_business_date_changed')
            receipt.update(status='running', current_stage=item['stage'])
            item.update(status='running', started_at=store.timestamp_factory())
            store.write(receipt)  # Before effects: a crash is uncertain, never pending replay.
            proof = action()
            if not isinstance(proof, StageProof) or not proof.versions or not all(isinstance(v,str) and v for v in proof.versions.values()):
                raise CycleStageFailure('cycle_stage_proof_missing:' + item['stage'])
            item.update(status='degraded' if proof.warnings else 'complete', finished_at=store.timestamp_factory(),
                versions=proof.versions, warnings=list(proof.warnings))
            receipt['final_versions'].update(proof.versions)
            store.write(receipt)
        receipt.update(status='degraded' if any(i['status']=='degraded' for i in receipt['stages']) else 'complete',
            finished_at=store.timestamp_factory(), current_stage='')
        store.write(receipt)
        return receipt
    except BaseException as exc:
        # Small codes only: exception text can contain a provider response/secret.
        code = str(exc) if isinstance(exc, CycleStageFailure) else type(exc).__name__
        receipt.update(status='failed' if isinstance(exc, Exception) else 'interrupted',
            error_code=code[:256], finished_at=store.timestamp_factory())
        for item in receipt['stages']:
            if item['status'] == 'running':
                item.update(status=receipt['status'], error_code=code[:256], finished_at=store.timestamp_factory())
        store.write(receipt)
        raise
