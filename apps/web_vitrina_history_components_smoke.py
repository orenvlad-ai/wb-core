"""Owned native component proofs: bounded initial warming without weaker hashes."""
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_history_compiler_smoke import capture_inventory
from packages.application import sheet_vitrina_v1_inventory_history as inventory
from packages.application import web_vitrina_history_live_adapter as live
from packages.application.ready_publication import ensure_publication_schema
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler, digest
from packages.application.web_vitrina_history_store import HistoryStore
from packages.application.web_vitrina_window_read_context import window_read_context

NOW = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
COMPONENTS = inventory.COMPONENTS_TABLE
PREFIX = COMPONENTS + ':'
LIMIT = 32 * 1024**2


def make_adapter(runtime, cache):
    return live.LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
        cache_dir=cache, now=NOW, date_from='2026-04-14', date_to='2026-04-20',
        formula_epoch='component-proof-fixture')


def cache_entries(adapter):
    path = adapter.cache_dir / 'source-proofs.json'
    return json.loads(path.read_text())['slices'] if path.exists() else {}


def warm(adapter):
    passes, pending = 0, []
    while True:
        passes += 1
        try:
            vector = adapter.capture()
        except live.LiveSourceUnavailable as exc:
            assert str(exc) in {'live_ready_bootstrap_pending', 'live_dated_bootstrap_pending',
                               'live_components_bootstrap_pending'}
            assert (adapter.stats['plans_loaded'] or adapter.stats['dated_days_loaded'] or
                    adapter.stats['component_captures_loaded'])
            pending.append((str(exc), adapter.stats['component_captures_loaded']))
            assert passes < 10
        else:
            return vector, passes, pending


def check_native(root):
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=7, now=NOW)
    with fixture, ExitStack() as resources:
        runtime = fixture.entrypoint.runtime
        append = inventory.append_inventory_history_capture
        def padded(*args, **kwargs):
            components = [dict(v) for v in kwargs['components']]
            components[0]['provenance'] = {**components[0]['provenance'],
                                         'unused_fixture_padding': 'x' * (6 * 1024**2)}
            return append(*args, **{**kwargs, 'components': components})
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')  # Provision own native family.
            inventory.ensure_inventory_history_schema(conn)
            for day in ['2026-04-' + str(i) for i in range(14, 21)]:
                # Retained superseded native captures still require full proofs.
                # Latest finalization stays small enough for real 16-field cells.
                with patch.object(inventory, 'append_inventory_history_capture', side_effect=padded):
                    capture_inventory(conn, day, 'retained-facility', revision='retained')
                capture_inventory(conn, day, 'retained-facility', revision='current')
            ensure_publication_schema(conn)
            sizes = dict(conn.execute('SELECT capture_id,sum(length(CAST(provenance_json AS BLOB))) '
                                      'FROM ' + COMPONENTS + ' GROUP BY capture_id'))
            assert sum(sizes.values()) > LIMIT and max(sizes.values()) < LIMIT
        keeper = resources.enter_context(closing(sqlite3.connect(runtime.db_path)))
        keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
        adapter = make_adapter(runtime, root / 'proofs')
        vector, passes, pending = warm(adapter)
        assert any(code == 'live_components_bootstrap_pending' and count > 0 for code, count in pending)
        # Independently hash every complete ordered native row, not a projection.
        expected = {}
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            for capture_id, source_digest in conn.execute('SELECT capture_id,source_digest FROM ' + inventory.CAPTURES_TABLE):
                rows = [list(row) for row in conn.execute('SELECT * FROM ' + COMPONENTS +
                    ' WHERE capture_id=? ORDER BY scope_key,component_kind,component_id', (capture_id,))]
                expected[PREFIX + capture_id + ':' + source_digest] = digest(rows)
        entries = cache_entries(adapter)
        assert all(entries[k]['proof'] == proof for k, proof in expected.items())
        assert len(expected) == 14
        assert adapter.capture() == vector and adapter.stats['component_captures_loaded'] == 0
        assert adapter.stats['dated_days_loaded'] == adapter.stats['plans_loaded'] == 0
        # Previously accepted complete cache entries require no schema migration.
        entries_before = {k: v for k, v in cache_entries(adapter).items() if k.startswith(PREFIX)}
        reopened = make_adapter(runtime, adapter.cache_dir)
        assert reopened.capture() == vector and reopened.stats['component_captures_loaded'] == 0
        assert {k: v for k, v in cache_entries(reopened).items() if k.startswith(PREFIX)} == entries_before

        def compiled(source):
            with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
                seen = source.capture()
                compiler = NativeDatedCompiler(runtime, NOW, source.days[0], source.days[-1],
                    prepared_context=source.context, prepared_availability=source.availability,
                    lifecycle_quality_resolver=source.lifecycle_quality_resolver)
                units = {day: compiler.compile(day) for day in (source.days[0], source.days[-1])}
                source.finish_quality_portion()
                return seen, compiler.catalog, units
        reference = make_adapter(runtime, root / 'reference')
        reference.max_read_bytes = 128 * 1024**2  # Owned offline oracle only.
        ref_vector, ref_catalog, ref_units = compiled(reference)
        seen, catalog, units = compiled(adapter)
        assert seen == ref_vector == vector and catalog == ref_catalog and units == ref_units
        assert all(len(cell) == 16 for unit in units.values() for cell in unit['cells'].values())
        cells = sum(len(unit['cells']) for unit in units.values())
        # Actual initial bootstrap integration, within the unchanged portion cap.
        initial = make_adapter(runtime, root / 'initial')
        store = HistoryStore(root / 'history')
        result = live.update_live_history(adapter=initial, runtime=runtime, store=store,
            max_recomputes=31, deadline_monotonic=time.monotonic() + 180)
        assert result['status'] == 'published' and len(store.edition()['days']) == 7
        assert result['bootstrap_source_reads']['captures'] >= 2
        before_pointer = (store.root / 'CURRENT.json').read_bytes()
        before_fence = adapter.fence
        # Supported append/finalize changes proof and fence, without cache edits.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            capture_inventory(conn, adapter.days[0], 'retained-facility', revision='correction')
        changed, _, _ = warm(adapter)
        assert {d for d in vector['dates'] if vector['dates'][d] != changed['dates'][d]} == {adapter.days[0]}
        assert changed['epoch'] == vector['epoch']
        assert adapter.fence != before_fence
        # Even real cache progress must not retry portion/publication captures.
        for failing_call in (2, 3):
            actual = adapter.capture
            calls = []
            def refuse_later():
                calls.append(1)
                if len(calls) == failing_call:
                    adapter.stats = {'bytes': LIMIT + 1, 'queries': 1, 'plans_loaded': 0,
                                     'dated_days_loaded': 0, 'component_captures_loaded': 1}
                    raise live.LiveSourceUnavailable('live_components_bootstrap_pending')
                return actual()
            with patch.object(adapter, 'capture', side_effect=refuse_later):
                try:
                    live.update_live_history(adapter=adapter, runtime=runtime, store=store,
                        deadline_monotonic=time.monotonic() + 180)
                except live.LiveSourceUnavailable as exc:
                    assert str(exc) == 'live_components_bootstrap_pending'
                else:
                    raise AssertionError('only initial bootstrap can retry')
            assert len(calls) == failing_call and (store.root / 'CURRENT.json').read_bytes() == before_pointer

        # Byte failure after complete proofs leaves the interrupted key absent.
        cold = make_adapter(runtime, root / 'partial')
        try:
            cold.capture()
        except live.LiveSourceUnavailable as exc:
            assert str(exc) == 'live_components_bootstrap_pending'
        else:
            raise AssertionError('cold aggregate must need another bounded capture')
        completed = {k: v for k, v in cache_entries(cold).items() if k.startswith(PREFIX)}
        assert 0 < len(completed) < len(expected) and cold.stats['component_captures_loaded'] == len(completed)
        assert all(v['proof'] == expected[k] for k, v in completed.items())
        failure_checks = check_failures(runtime, root, store, before_pointer)
        return {'aggregate_provenance_bytes': sum(sizes.values()), 'largest_capture_provenance_bytes': max(sizes.values()),
            'cold_passes': passes, 'complete_ordered_capture_proofs': len(expected), 'warm_new_component_proofs': 0,
            'old_cache_reused': True, 'reference_vector_catalog_cells_16_equal': cells,
            'actual_initial_bootstrap_published_days': len(store.edition()['days']),
            'correction_exact_day_and_fence': True, 'portion_publication_captures_not_retried': True,
            'partial_capture_not_cached': True, 'failures': failure_checks}


def check_failures(runtime, root, store, before_pointer):
    # Actual source capture with injected resource/durability boundaries.
    pending_path = store.root / 'PENDING.json'
    pending_before = pending_path.read_bytes() if pending_path.exists() else None
    for failure in ('oversized', 'time', 'byte_and_time', 'sqlite_interrupt', 'durability'):
        adapter = make_adapter(runtime, root / ('failure-' + failure))
        original = adapter._rows
        component_reads = []
        def bounded_rows(conn, sql, args=()):
            if sql.startswith('SELECT * FROM ' + COMPONENTS):
                component_reads.append(args[0])
                if failure == 'oversized' or (failure in ('time', 'byte_and_time', 'sqlite_interrupt') and len(component_reads) == 2):
                    if failure == 'sqlite_interrupt':
                        raise sqlite3.OperationalError('interrupted')
                    if failure in ('time', 'byte_and_time'):
                        adapter.deadline = time.monotonic() - 1
                    if failure in ('oversized', 'byte_and_time'):
                        adapter.stats['bytes'] = LIMIT + 1
                    # Oversized first capture has no component progress, but
                    # initial ready/day progress may legitimately warm once.
                    if failure == 'oversized':
                        adapter.stats['plans_loaded'] = adapter.stats['dated_days_loaded'] = 0
                    raise live.LiveSourceUnavailable('live_source_resource_limit')
            return original(conn, sql, args)
        atom = live._atomic
        def durable(path, value):
            if path.name == 'source-proofs.json' and failure == 'durability':
                raise OSError('owned persistence failure')
            return atom(path, value)
        with patch.object(adapter, '_rows', side_effect=bounded_rows), patch.object(live, '_atomic', side_effect=durable):
            try:
                live.update_live_history(adapter=adapter, runtime=runtime, store=store,
                    deadline_monotonic=time.monotonic() + 180)
            except (live.LiveSourceUnavailable, OSError) as exc:
                if failure == 'durability':
                    assert isinstance(exc, OSError) and not (adapter.cache_dir / 'source-proofs.json').exists()
                else:
                    assert str(exc) == ('live_source_read_incomplete' if failure == 'sqlite_interrupt' else 'live_source_resource_limit')
            else:
                raise AssertionError('terminal boundary must not resume')
        assert adapter.capture_calls == 1 and (store.root / 'CURRENT.json').read_bytes() == before_pointer
        assert (pending_path.read_bytes() if pending_path.exists() else None) == pending_before
        proofs = {k: v for k, v in cache_entries(adapter).items() if k.startswith(PREFIX)}
        assert len(proofs) == (1 if failure in ('time', 'byte_and_time', 'sqlite_interrupt') else 0)
    # No-progress pending is terminal, even if an erroneous source emits it.
    adapter = make_adapter(runtime, root / 'no-progress')
    def no_progress():
        adapter.capture_calls += 1
        adapter.stats = {'bytes': 0, 'queries': 0, 'plans_loaded': 0,
                         'dated_days_loaded': 0, 'component_captures_loaded': 0}
        raise live.LiveSourceUnavailable('live_components_bootstrap_pending')
    with patch.object(adapter, 'capture', side_effect=no_progress):
        try:
            live.update_live_history(adapter=adapter, runtime=runtime, store=store,
                                    deadline_monotonic=time.monotonic() + 180)
        except live.LiveSourceUnavailable as exc:
            assert str(exc) == 'live_components_bootstrap_pending'
        else:
            raise AssertionError('no-progress must refuse')
    assert adapter.capture_calls == 1 and (store.root / 'CURRENT.json').read_bytes() == before_pointer
    assert (pending_path.read_bytes() if pending_path.exists() else None) == pending_before
    return {'oversized_no_progress_terminal': True, 'expired_time_and_byte_time_terminal': True,
            'sqlite_interruption_terminal': True, 'atomic_persistence_failure_terminal': True,
            'incomplete_capture_absent': True, 'last_good_and_pending_retained': True, 'pending_no_progress_terminal': True}


def check_actual_oversized(root):
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=1, now=NOW)
    with fixture, ExitStack() as resources:
        runtime = fixture.entrypoint.runtime
        append = inventory.append_inventory_history_capture
        def oversized(*args, **kwargs):
            components = [dict(v) for v in kwargs['components']]
            components[0]['provenance'] = {**components[0]['provenance'],
                                         'unused_fixture_padding': 'x' * (33 * 1024**2)}
            return append(*args, **{**kwargs, 'components': components})
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')
            inventory.ensure_inventory_history_schema(conn)
            with patch.object(inventory, 'append_inventory_history_capture', side_effect=oversized):
                capture_inventory(conn, '2026-04-20', 'retained-facility')
            ensure_publication_schema(conn)
            capture_id, source_digest = conn.execute('SELECT capture_id,source_digest FROM ' + inventory.CAPTURES_TABLE).fetchone()
            size = conn.execute('SELECT sum(length(CAST(provenance_json AS BLOB))) FROM ' + COMPONENTS).fetchone()[0]
            assert size > LIMIT
        keeper = resources.enter_context(closing(sqlite3.connect(runtime.db_path)))
        keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
        adapter = make_adapter(runtime, root / 'oversized-native')
        # Complete unrelated ready/day proofs can warm once. The oversized
        # first component capture then fails without repeatable new progress.
        try:
            adapter.capture()
        except live.LiveSourceUnavailable as exc:
            assert str(exc) == 'live_dated_bootstrap_pending'
            assert adapter.stats['dated_days_loaded'] or adapter.stats['plans_loaded']
        else:
            raise AssertionError('oversized native capture must refuse')
        key = PREFIX + capture_id + ':' + source_digest
        assert key not in cache_entries(adapter) and adapter.stats['component_captures_loaded'] == 0
        for _ in range(2):
            try:
                adapter.capture()
            except live.LiveSourceUnavailable as exc:
                assert str(exc) == 'live_source_resource_limit'
            else:
                raise AssertionError('oversized capture cannot claim resumable progress')
            assert key not in cache_entries(adapter)
            assert adapter.stats['component_captures_loaded'] == adapter.stats['dated_days_loaded'] == adapter.stats['plans_loaded'] == 0
        return {'single_capture_provenance_bytes': size, 'full_proof_never_cached': True,
                'warm_no_progress_remains_terminal': True}


def main():
    with tempfile.TemporaryDirectory(prefix='inventory-component-proof-') as temp:
        print(json.dumps({'status': 'PASS', 'native': check_native(Path(temp)),
                          'actual_oversized': check_actual_oversized(Path(temp))}, sort_keys=True))


if __name__ == '__main__':
    main()
