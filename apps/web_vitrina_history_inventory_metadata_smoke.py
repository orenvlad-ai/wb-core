"""Owned native proofs: compact inventory metadata without semantic loss."""
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
from packages.application.ready_publication import ensure_publication_schema
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler, digest
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, LiveSourceUnavailable
from packages.application.web_vitrina_window_read_context import window_read_context

TABLE = 'sheet_vitrina_v1_inventory_history_captures'
BOUND = 'bound_inventory_quantity_v1'
NOW = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)


def reset(adapter):
    adapter.stats = {'bytes': 0, 'queries': 0, 'plans_loaded': 0, 'dated_days_loaded': 0}
    adapter.deadline = time.monotonic() + 20
    adapter.used_slices = set()


def legacy_captures(adapter, conn):
    return adapter._rows(conn, 'SELECT capture_id,business_date,facility_roster_json,'
        'source_manifest_json,source_digest FROM ' + TABLE +
        ' WHERE business_date>=? AND business_date<=? ORDER BY capture_sequence',
        (adapter.days[0], adapter.days[-1]))


def check_classification(root):
    adapter = LiveNativeAdapter(db_path=root / 'native', runtime_dir=root / 'runtime',
        cache_dir=root / 'cache', now=NOW, date_from='2026-04-14',
        date_to='2026-04-20', formula_epoch='metadata-fixture')
    cases = [
        {}, {'contract': BOUND}, {'contract': 'other'}, {'contract': None},
        {'contract': {}}, {'contract': []}, {'contract': True}, {'contract': False},
        {'contract': 1}, {'contract': 0}, {'contract': 1.5},
    ]
    manifests = [json.dumps(v) for v in cases] + [
        '{"contract":"other","contract":"' + BOUND + '"}',
        '{"contract":"' + BOUND + '","contract":"other"}',
        '{"nested":{"contract":"' + BOUND + '"}}',
        '{"contract":"' + BOUND + '","contract":null}',
        '{"con\\u0074ract":"other","contract":"' + BOUND + '"}',
        '{"contract":"' + BOUND + '","con\\u0074ract":"other"}',
    ]
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE ' + TABLE + '(capture_sequence INTEGER PRIMARY KEY,'
            'capture_id TEXT,business_date TEXT,facility_roster_json TEXT,'
            'source_manifest_json TEXT,source_digest TEXT)')
        for i, manifest in enumerate(manifests):
            conn.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?,?,?)',
                (i, str(i), adapter.days[-1] if i % 2 else adapter.days[0],
                 json.dumps([{'facility_id': 'same', 'name': str(i)}]), manifest, 'digest-' + str(i)))
        reset(adapter)
        old = legacy_captures(adapter, conn)
        reset(adapter)
        new = adapter._inventory_captures(conn)
        assert len(new) == len(old) == len(manifests)
        assert [row[0] for row in new] == [str(i) for i in range(len(manifests))]
        for before, after in zip(old, new):
            assert before[:3] == after[:3] and before[4] == after[4]
            assert (json.loads(before[3]).get('contract') == BOUND) == (
                json.loads(after[3]).get('contract') == BOUND)
        # Full row proof still consumes every field, including unused manifest.
        cache = {'slices': {}}
        reset(adapter)
        first = adapter._slice(conn, {TABLE}, cache, {TABLE: 1}, TABLE, 'business_date', adapter.days)
        conn.execute('UPDATE ' + TABLE + ' SET source_manifest_json=? WHERE capture_sequence=0',
            ('{"unused":"semantic correction"}',))
        reset(adapter)
        second = adapter._slice(conn, {TABLE}, cache, {TABLE: 2}, TABLE, 'business_date', adapter.days)
        assert {day for day in first if first[day] != second[day]} == {adapter.days[0]}
        for encoded in ['not json', 'null', '[]', '"text"', 'true', '42']:
            conn.execute('UPDATE ' + TABLE + ' SET source_manifest_json=? WHERE capture_sequence=0', (encoded,))
            reset(adapter)
            try:
                adapter._inventory_captures(conn)
            except LiveSourceUnavailable as exc:
                assert str(exc) == 'live_inventory_capture_manifest_invalid'
            else:
                raise AssertionError('invalid/nonobject manifest must fail closed')
        conn.execute('UPDATE ' + TABLE + ' SET source_manifest_json=? WHERE capture_sequence=0', ('{}',))
        for bytes_limit, expired in [(1, False), (32 * 1024**2, True)]:
            reset(adapter)
            adapter.max_read_bytes = bytes_limit
            if expired:
                adapter.deadline = time.monotonic() - 1
            try:
                adapter._inventory_captures(conn)
            except LiveSourceUnavailable as exc:
                assert str(exc) == 'live_source_resource_limit'
            else:
                raise AssertionError('projection must retain byte/time refusal')
    return {'contract_cases': len(manifests), 'all_roster_digest_order_preserved': True,
            'full_unused_manifest_proof': True, 'malformed_nonobject_refusal': True,
            'byte_time_caps': True}


def check_native_bulk(root):
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=7, now=NOW)
    with fixture, ExitStack() as resources:
        runtime = fixture.entrypoint.runtime
        # Native producer creates valid digest/component/finalization receipts.
        # Aggregate unused content exceeds 32MiB, each full day remains bounded.
        append = inventory.append_inventory_history_capture
        def padded(*args, **kwargs):
            kwargs['source_manifest'] = {**kwargs['source_manifest'],
                'unused_fixture_padding': 'x' * (6 * 1024**2)}
            return append(*args, **kwargs)
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')  # Own fixture provisions the family.
            inventory.ensure_inventory_history_schema(conn)
            with patch.object(inventory, 'append_inventory_history_capture', side_effect=padded):
                for day in ['2026-04-' + str(i) for i in range(14, 21)]:
                    capture_inventory(conn, day, 'retained-facility')
            ensure_publication_schema(conn)
            raw_bytes = conn.execute('SELECT sum(length(CAST(source_manifest_json AS BLOB))) FROM ' + TABLE).fetchone()[0]
            assert raw_bytes > 32 * 1024**2
        keeper = resources.enter_context(closing(sqlite3.connect(runtime.db_path)))
        keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
        adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
            cache_dir=root / 'native-proofs', now=NOW, date_from='2026-04-14',
            date_to='2026-04-20', formula_epoch='metadata-fixture')
        assert adapter.max_read_bytes == 32 * 1024**2 and adapter.max_capture_seconds == 20
        cold_passes = 0
        while True:
            cold_passes += 1
            try:
                vector = adapter.capture()
            except LiveSourceUnavailable as exc:
                assert str(exc) in {'live_ready_bootstrap_pending', 'live_dated_bootstrap_pending'}
                assert adapter.stats['plans_loaded'] or adapter.stats['dated_days_loaded']
                assert cold_passes < 10
            else:
                break
        assert cold_passes >= 2
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            columns = [row[1] for row in conn.execute('PRAGMA table_info(' + TABLE + ')')]
            alarm = conn.execute('SELECT revision FROM sheet_vitrina_v1_ready_input_revisions WHERE source_table=?', (TABLE,)).fetchone()[0]
            cache = json.loads((adapter.cache_dir / 'source-proofs.json').read_text())
            for day in adapter.days:
                rows = [list(row) for row in conn.execute('SELECT * FROM ' + TABLE +
                    ' WHERE business_date=? ORDER BY ' + ','.join('"' + c + '"' for c in columns), (day,))]
                assert cache['slices'][digest([TABLE, alarm, day])] == digest(rows)
        assert adapter.capture() == vector and adapter.stats['plans_loaded'] == 0
        assert adapter.stats['dated_days_loaded'] == 0 and adapter.stats['bytes'] < 32 * 1024**2
        # Old warm metadata path reproduces the rejection at unchanged cap.
        with patch.object(LiveNativeAdapter, '_inventory_captures', legacy_captures):
            try:
                adapter.capture()
            except LiveSourceUnavailable as exc:
                assert str(exc) == 'live_source_resource_limit'
            else:
                raise AssertionError('legacy full metadata must exceed the same cap')
        # Larger cap is an OFFLINE reference oracle, never the repaired runtime.
        reference = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
            cache_dir=adapter.cache_dir, now=NOW, date_from=adapter.days[0],
            date_to=adapter.days[-1], formula_epoch='metadata-fixture', max_read_bytes=128 * 1024**2)
        def compile_units(source, legacy=False):
            with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
                with patch.object(LiveNativeAdapter, '_inventory_captures', legacy_captures) if legacy else ExitStack():
                    seen = source.capture()
                    compiler = NativeDatedCompiler(runtime, NOW, source.days[0], source.days[-1],
                        prepared_context=source.context, prepared_availability=source.availability,
                        lifecycle_quality_resolver=source.lifecycle_quality_resolver)
                    units = {day: compiler.compile(day) for day in [source.days[0], source.days[-1]]}
                    source.finish_quality_portion()
                    return seen, compiler.catalog, units
        legacy_vector, legacy_catalog, legacy_units = compile_units(reference, legacy=True)
        fresh_vector, fresh_catalog, fresh_units = compile_units(adapter)
        assert legacy_vector == fresh_vector == vector
        assert legacy_catalog == fresh_catalog and legacy_units == fresh_units
        cells = sum(len(unit['cells']) for unit in fresh_units.values())
        assert cells and all(len(cell) == 16 for unit in fresh_units.values() for cell in unit['cells'].values())
        # Same alarm/fence contract: an unused-field correction still changes
        # the full day proof; projections never become content authority.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            def corrected_append(*args, **kwargs):
                kwargs['source_manifest'] = {**kwargs['source_manifest'],
                    'unused_semantic_correction': 'changed'}
                return padded(*args, **kwargs)
            with patch.object(inventory, 'append_inventory_history_capture', side_effect=corrected_append):
                capture_inventory(conn, adapter.days[0], 'retained-facility')
        for _ in range(10):
            try:
                corrected = adapter.capture()
            except LiveSourceUnavailable as exc:
                assert str(exc) == 'live_dated_bootstrap_pending' and adapter.stats['dated_days_loaded']
            else:
                break
        else:
            raise AssertionError('corrected full day proofs must finish bounded warming')
        assert {day for day in vector['dates'] if vector['dates'][day] != corrected['dates'][day]} == {adapter.days[0]}
        assert corrected['epoch'] == vector['epoch']
        return {'manifest_bytes': raw_bytes, 'cold_passes': cold_passes,
                'full_day_proofs_verified': len(adapter.days), 'old_path_same_cap_refused': True,
                'legacy_new_vectors_catalog_cells_equal': True, 'cells_16_fields': cells,
                'unused_field_correction_visible': True, 'production_caps_unchanged': True}


def main():
    with tempfile.TemporaryDirectory(prefix='inventory-metadata-proof-') as temp:
        root = Path(temp)
        print(json.dumps({'status': 'PASS', 'classification': check_classification(root),
                          'native_bulk': check_native_bulk(root)}, sort_keys=True))


if __name__ == '__main__':
    main()
