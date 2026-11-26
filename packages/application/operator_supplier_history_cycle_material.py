"""Carry an exact supplier-owned book revision through the cycle's daily Finance proof."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, timezone
import json
from pathlib import Path
from time import monotonic

from packages.application.ready_publication import canonical, digest


def _require(value, reason):
    from packages.application.sheet_vitrina_v1_cycle import CycleStageFailure
    if not value:
        raise CycleStageFailure('supplier_cycle_' + reason)


@dataclass(frozen=True)
class SupplierMaterial:
    cycle: str
    job: str
    before_book: str
    after_book: str
    before_daily: str
    after_daily: str
    operation: str
    attempt: str
    manifest_digest: str
    dates: tuple[str, ...]
    prepare_ms: int
    writer_ms: int

    def material(self, receipt):
        original = receipt['final_versions']
        _require((receipt['cycle_id'], receipt['job_id']) == (self.cycle, self.job)
                 and (original['fbs_book'], original['daily_projection_digest']) ==
                 (self.before_book, self.before_daily), 'material_owner_changed')
        return {**original, 'fbs_book': self.after_book, 'daily_projection_digest': self.after_daily}

    def attest(self, supplier, history, ack, finance):
        _require((supplier.operation_id, supplier.attempt_id, ack['manifest_digest']) ==
                 (self.operation, self.attempt, self.manifest_digest)
                 and history.get('supplier_ack') is True and ack['native']
                 and set(ack['native']) == set(supplier.dates)
                 and all(v['edition_id'] == history['edition_id'] for v in ack['native'].values())
                 and finance['accounting_version_before'] == finance['accounting_version'] == self.after_book,
                 'material_completion_changed')

    def publication(self):
        return {'before_book': self.before_book, 'after_book': self.after_book,
                'before_daily': self.before_daily, 'after_daily': self.after_daily,
                'operation_id': self.operation, 'attempt_id': self.attempt,
                'manifest_digest': self.manifest_digest, 'recalculated_days': list(self.dates),
                'prepare_ms': self.prepare_ms, 'writer_ms': self.writer_ms}


def source_predecessors(receipt):
    """Recover the two domain proofs, including an admitted in-cycle FBS refresh."""
    selected = [row for row in receipt.get('stages', ()) if row['stage'] in ('finance_sources', 'fbs_generation')]
    _require(len(selected) == 2 and {row['stage'] for row in selected} == {'finance_sources', 'fbs_generation'}
             and all(row['status'] in ('complete', 'degraded') for row in selected), 'source_stage_unproven')
    stages = {row['stage']: row['versions'] for row in selected}
    finance, fbs = stages['finance_sources'], stages['fbs_generation']
    refresh = receipt.get('fbs_catalog_refresh')
    if refresh:
        _require(refresh.get('status') == 'complete' and refresh.get('prior_versions') == fbs,
                 'source_stage_refresh_changed')
        fbs = {**fbs, **refresh['versions']}
    _require(finance and fbs and all(receipt['final_versions'].get(key) == value
             for domain in (finance, fbs) for key, value in domain.items()), 'source_stage_binding_changed')
    return finance, fbs


def _versions(connection):
    versions = []
    for _, schema, filename in connection.execute('PRAGMA database_list'):
        if schema == 'temp':
            continue
        _require(schema in ('main', 'finance_daily_raw_store'), 'daily_store_changed')
        path = Path(filename)
        identity = path.stat()
        version = connection.execute(f'PRAGMA {schema}.data_version').fetchone()[0]
        versions.append((schema, path, identity.st_dev, identity.st_ino, version))
    return versions


def _edge(ledger, manifest, before):
    active = ledger.execute('SELECT version FROM accounting_current WHERE singleton=1').fetchone()
    if active and active[0] == before:
        return False
    after = manifest['after_book']
    row = ledger.execute('SELECT previous_version,version FROM accounting_revisions WHERE operation_id=?',
                         (manifest['book_operation_id'],)).fetchone()
    _require(active == (after,) and manifest['expected_book'] == before and row == (before, after),
             'book_transition_changed')
    return True


def _old_reader(block, version, now):
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.wb_finance_daily import WbFinanceDailyBlock
    old = accounting.load_shared(block.runtime_dir, bound_book=accounting.load(block.runtime_dir, version=version))
    _require(old is not None, 'previous_book_unavailable')

    class PublishedBookDaily(WbFinanceDailyBlock):
        # Keep the normal reporting overlay and live dependency fingerprints.
        # Only the immutable revision supplying the shared book is pinned.
        def _load_published_cost_book(self):
            return old

    return PublishedBookDaily(block.runtime_dir, seller_id=block.seller_id, now_factory=lambda: now)


@contextmanager
def _reserve_raw(registry, generation):
    if (registry.resolve('finance_raw', manifest=generation) ==
            registry.resolve('operational', manifest=generation)):
        yield
        return
    with registry.session('finance_raw', mode='rw', manifest=generation,
                          operation='supplier_cycle_raw_reservation', timeout_ms=1500,
                          isolation_level=None) as raw:
        raw.execute('BEGIN IMMEDIATE')
        try:
            yield
        finally:
            raw.rollback()


def _daily_image(ingress, prepared):
    """Render native derived fields using the unchanged, accepted WB batch metadata."""
    _require(ingress['status'] in ('completed', 'loaded_preliminary') and 'metrics' in prepared
             and prepared['raw_row_count'] == ingress['raw_row_count']
             and prepared['report_ids'] == ingress['report_ids']
             and prepared['report_types'] == ingress['report_types'], 'daily_raw_changed')
    coverage = {**prepared['coverage'], 'spp': prepared['spp']}
    return {**ingress, 'metrics': prepared['metrics'],
            'cost_coverage': json.loads(json.dumps(coverage, default=str)),
            'unknown_reasons': prepared['unknown']}


def _prepare_aggregates(block, reader, previous):
    from packages.application.canonical_wb_cost_resolver import CanonicalChannelCostSnapshot

    original = previous.build_daily_payload()['days']
    current = block.build_daily_payload()['days']
    _require([row['day'] for row in current] == [row['day'] for row in original], 'daily_scope_changed')
    # Release each SELECT's read lock in rollback-journal mode. The same
    # connections' data versions fence the whole preparation.
    _require(not reader.in_transaction, 'daily_reader_not_idle')
    started = monotonic()
    snapshot = CanonicalChannelCostSnapshot.from_connection(reader) if current else None
    updates, expected = [], []
    for index, (old, active) in enumerate(zip(original, current, strict=True)):
        day = date.fromisoformat(old['day'])
        if active['status'] == 'stale_projection':
            prepared = block._prepare_projection(reader, day, cost_snapshot=snapshot)
            expected.append(_daily_image(old, prepared))
            updates.append(prepared)
        else:
            if old['status'] == 'stale_projection':
                # The aggregate already points at the new book. Rebuild both
                # sides from the same WB rows before accepting a write-free repeat.
                native = block._prepare_projection(reader, day, cost_snapshot=snapshot)
                _require(_daily_image(active, native) == active, 'daily_repair_readback_changed')
                prior = previous._prepare_projection(reader, day, cost_snapshot=snapshot)
                original[index] = _daily_image(active, prior)
            expected.append(active)
    return original, updates, digest(canonical(expected)), round((monotonic() - started) * 1000)


def _commit_aggregates(block, generation, updates, verify_sources, clock, business_day):
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.sheet_vitrina_v1_cycle import require_cycle_business_date
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock

    if not updates:
        verify_sources()
        return 0
    with (warehouse_functional_write_lock(block.runtime_dir, timeout_seconds=5), accounting.writer_lock(block.runtime_dir),
          _reserve_raw(block.store_registry, generation)):
        with block.store_registry.session('operational', mode='rw', manifest=generation,
                operation='supplier_cycle_daily_projection', timeout_ms=1500, isolation_level=None) as writer:
            writer.execute('BEGIN IMMEDIATE')
            started = monotonic()
            verify_sources()
            checked = clock()
            require_cycle_business_date(business_day, checked)
            timestamp = checked.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            for prepared in updates:
                block._write_daily_aggregate(writer, prepared, timestamp)
            require_cycle_business_date(business_day, clock())
            writer.commit()
            return round((monotonic() - started) * 1000)


def prepare_supplier_material(entry, receipt, supplier):
    """Repair only stale derived daily aggregates before History takes its source vector."""
    from apps.wb_finance_daily import _worker_lock
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.sheet_vitrina_v1_cycle import require_cycle_business_date
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner

    runtime = entry.runtime.runtime_dir
    _require(require_heavy_owner(runtime).operation == 'cycle', 'owner_required')
    require_warehouse_job_owner(runtime)
    source, _ = supplier._read()
    bounds = receipt['final_versions']
    finance, fbs = source_predecessors(receipt)
    before, after = bounds['fbs_book'], source['after_book']
    block = entry.wb_finance_daily_block
    with accounting.readonly_book_connection(runtime) as ledger:
        if not _edge(ledger, source, before):
            return None
        ledger_version = _versions(ledger)
        now = entry.now_factory()
        require_cycle_business_date(receipt['business_date'], now)
        supplier_sources = supplier.validate_sources_readonly(now=now)
        _require(supplier_sources['active_book'] == after, 'book_transition_changed')
        with _worker_lock(runtime) as admitted:
            _require(admitted, 'daily_worker_busy')
            generation = block.store_registry.load()
            with closing(block._connect_daily_read()) as reader:
                tokens = _versions(reader)
                # Recover both sides from accepted raw reports on an already-applied retry.
                previous = _old_reader(block, before, now)
                original, updates, expected, prepare_ms = _prepare_aggregates(block, reader, previous)
                entry._cycle_validate_predecessors(finance, fbs, {**bounds, 'fbs_book': after},
                                                   daily_days=original)
                del previous, original

                def verify_sources():
                    _require(generation == block.store_registry.load() and _versions(reader) == tokens
                             and _versions(ledger) == ledger_version and _edge(ledger, source, before),
                             'daily_sources_changed_before_commit')

                writer_ms = _commit_aggregates(block, generation, updates, verify_sources,
                                                entry.now_factory, receipt['business_date'])
                _require(generation == block.store_registry.load() and _edge(ledger, source, before)
                         and digest(canonical(block.build_daily_payload()['days'])) == expected,
                         'daily_repair_readback_changed')
        _require(supplier.validate_sources_readonly(now=entry.now_factory()) == supplier_sources,
                 'supplier_sources_changed')
        return SupplierMaterial(receipt['cycle_id'], receipt['job_id'], before, after,
                                bounds['daily_projection_digest'], expected, supplier.operation_id,
                                supplier.attempt_id, source['manifest_digest'], tuple(row['day'].isoformat() for row in updates), prepare_ms, writer_ms)
