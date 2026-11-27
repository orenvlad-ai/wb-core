"""Native values stay authoritative; coverage comes from exact dated WB inputs."""
from copy import deepcopy
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import time
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_source_history_repair_smoke import fixture, observations, DAYS
from packages.application.registry_upload_db_backed_runtime import _to_namespace
from packages.application.web_vitrina_compact_table import CELL_FIELDS
from packages.application.web_vitrina_dated_source_quality import retained_scope_reader, verified_source_quality
from packages.application.web_vitrina_source_history_repair import project_day


def dependency_proof(root):
    from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
    adapter = LiveNativeAdapter(db_path=root/'native.sqlite3', runtime_dir=root/'runtime', cache_dir=root/'proofs',
        now=datetime(2026, 4, 20, tzinfo=timezone.utc), date_from=DAYS[0], date_to=DAYS[1], formula_epoch='fixture')
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE temporal_source_slot_snapshots(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,captured_at TEXT,payload_json TEXT)')
        conn.execute('CREATE TABLE sheet_vitrina_v1_ready_temporal_revisions(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,revision INTEGER)')
        for day in DAYS[:2]:
            for source in ('seller_funnel_snapshot', 'web_source_snapshot'):
                conn.execute('INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)',
                    (source, day, 'accepted_closed_day_snapshot', 'same-clock', '{"items":[]}'))
                conn.execute('INSERT INTO sheet_vitrina_v1_ready_temporal_revisions VALUES(?,?,?,?)',
                    (source, day, 'accepted_closed_day_snapshot', 1))
        cache = {'slices': {}}
        def capture():
            adapter.stats, adapter.deadline, adapter.used_slices = {'bytes': 0, 'queries': 0}, time.monotonic()+4, set()
            result = {day: {} for day in DAYS[:2]}
            adapter._temporal(conn, {'temporal_source_slot_snapshots','sheet_vitrina_v1_ready_temporal_revisions'},
                              cache, result, '2026-04-20')
            return result
        before = capture()
        for source in ('seller_funnel_snapshot', 'web_source_snapshot'):
            conn.execute('UPDATE temporal_source_slot_snapshots SET payload_json=? WHERE source_key=? AND snapshot_date=?',
                         ('{"items":[1]}', source, DAYS[0]))
            conn.execute('UPDATE sheet_vitrina_v1_ready_temporal_revisions SET revision=revision+1 WHERE source_key=? AND snapshot_date=?',
                         (source, DAYS[0]))
            after = capture()
            assert {d for d in before if before[d] != after[d]} == {DAYS[0]}
            before = after


def payloads(day, observed):
    return {o['source_key']: _to_namespace(dict(kind='success', count=len(o['items']), items=o['items'],
        source_fetched_at=o['source_fetched_at'], **({'date': day} if o['source_key']=='seller_funnel_snapshot'
                                                  else {'date_from': day, 'date_to': day}))) for o in observed}


def main():
    with TemporaryDirectory(prefix='dated-source-quality-') as raw:
        source, _, catalog, units, _ = fixture(Path(raw))
        day = DAYS[0]
        reference = retained_scope_reader(source, source.edition())(day)
        before_files = {str(p): p.read_bytes() for p in source.root.rglob('*') if p.is_file()}
        observed = observations(day)
        expected = project_day(day, catalog, units[day], observed, [100, 101], 'fixture')
        cells = {**deepcopy(units[day]['cells']), **deepcopy(expected)}
        # Reproduce normal refresh dropping typed SKU/CTR quality and adding
        # an undefined zero-denominator SKU to a TOTAL missing counter.
        for rid in expected:
            cells[rid][5:13] = ['', '', '', '', '', '', '', None]
        cells['TOTAL|total_view_count'][11:13] = ['partial', 1]
        cells['SKU:999|view_count'] = [999, '999', 'number', 'number_default', 'renderer:number:number_default',
                                     '', '', '', '', '', '', '', None, '', '', '']
        original = deepcopy(cells)
        current = payloads(day, observed)
        reads = []
        def load(**kwargs):
            assert kwargs['snapshot_date'] == day and kwargs['snapshot_role']=='accepted_closed_day_snapshot'
            reads.append(kwargs['source_key'])
            return current.get(kwargs['source_key']), '2026-11-12T08:01:00+00:00'
        runtime = SimpleNamespace(load_temporal_source_slot_snapshot=load)
        result, proof = verified_source_quality(runtime, day, cells, reference)
        assert cells == original and set(reads)==set(current)
        for rid, wanted in expected.items():
            assert result[rid][0:5] == original[rid][0:5]
            assert result[rid][5:13] == wanted[5:13], rid
        assert result['SKU:101|ctr'][8:13] == ['undefined', expected['SKU:101|ctr'][9],
            expected['SKU:101|ctr'][10], 'complete', 0]
        assert result['TOTAL|total_view_count'][11:13] == ['complete', 0]
        assert result['TOTAL|ctr'][11:13] == ['complete', 0]
        assert result['TOTAL|total_orders_current'][11:13] == ['partial', 1]
        assert result['GROUP:clean|total_orders_current'][11:13] == ['partial', 1]
        assert result['TOTAL|stock'] == original['TOTAL|stock']
        assert result['SKU:999|view_count'] == original['SKU:999|view_count']
        assert all(not s['mismatched_count'] for s in proof['sources'].values())
        # The next generation's current catalog is not new proof of dated
        # group membership. Reuse the explicit hash-bound historical scope,
        # without making last generation's object ID a semantic operand.
        next_root = Path(raw)/'next-generation'
        (next_root/'catalogs').mkdir(parents=True)
        changed_catalog = deepcopy(catalog)
        for rid, row in changed_catalog['rows'].items():
            if rid.startswith('SKU:'):
                row['group_id'] = 'group:today-only'
        (next_root/'catalogs'/'catalog.json').write_text(json.dumps(changed_catalog))
        retained_proof = proof
        def open_metadata(_):
            conn = sqlite3.connect(':memory:')
            conn.execute('CREATE TABLE metadata(payload TEXT)')
            conn.execute('INSERT INTO metadata VALUES(?)', (json.dumps({'date': day,
                'members': reference['members'], 'source_metric_quality': retained_proof}),))
            return conn
        next_store = SimpleNamespace(root=next_root, day_catalogs=lambda edition: {day:'catalog'}, _open_day=open_metadata)
        next_reference = retained_scope_reader(next_store, {'days':{day:'a-different-native-object'}})(day)
        assert next_reference['catalog']['rows']['SKU:100|view_count']['group_id'] == 'group:clean'
        _, next_proof = verified_source_quality(runtime, day, original, next_reference)
        assert next_proof['scope_sha256'] == proof['scope_sha256']
        assert next_proof['dated_scope'] == proof['dated_scope']
        # A retired native row/changed renderer roster is a local missing
        # projection, never an all-history failure or a fabricated new row.
        retired = deepcopy(original)
        retired.pop('SKU:101|ctr')
        no_assets = {**reference, 'catalog': {**reference['catalog'], 'presentation': {}}}
        retired_result, retired_proof = verified_source_quality(runtime, day, retired, no_assets)
        assert 'SKU:101|ctr' not in retired_result
        assert retired_result['TOTAL|total_view_count'][11:13] == ['complete', 0]
        assert retired_proof['sources']['seller_funnel_snapshot']['absent_native_count'] == 1
        partial_scope = {**reference, 'members': [r for r in reference['members'] if r != 'SKU:101|ctr']}
        scoped_result, scoped_proof = verified_source_quality(runtime, day, original, partial_scope)
        assert scoped_result['TOTAL|total_view_count'] == original['TOTAL|total_view_count']
        assert scoped_proof['sources']['seller_funnel_snapshot']['state'] == 'dated_family_rows_unproven'
        assert scoped_result['TOTAL|total_orders_current'][11:13] == ['partial', 1]
        # A catalog rebuild and a temporary missing observation must never
        # turn sparse historical membership into evidence for an entire family.
        retained_proof = scoped_proof
        sparse_reference = retained_scope_reader(next_store, {'days': {day: 'sparse'}})(day)
        second, second_proof = verified_source_quality(runtime, day, original, sparse_reference)
        assert second['TOTAL|total_view_count'] == original['TOTAL|total_view_count']
        assert second_proof['sources']['seller_funnel_snapshot']['state'] == 'dated_family_rows_unproven'
        no_data = SimpleNamespace(load_temporal_source_slot_snapshot=lambda **_: (None, None))
        _, retained_proof = verified_source_quality(no_data, day, original, sparse_reference)
        assert retained_proof['dated_scope'] == scoped_proof['dated_scope'] and not retained_proof['sources']
        assert verified_source_quality(runtime, day, cells, {**reference, 'members': []}) == (cells, None)
        # A newer genuinely changed exact-day report is accepted when the
        # native rebuild already uses it. Retained numbers are never read.
        newer = deepcopy(observed)
        newer[0]['items'][0]['view_count'] = 5
        current.update(payloads(day, newer))
        fresh = {**original, **project_day(day, catalog, units[day], newer, [100,101], 'fresh')}
        fresh_result, _ = verified_source_quality(runtime, day, fresh, reference)
        assert fresh_result['SKU:100|view_count'][0] == 5
        assert fresh_result['TOTAL|total_view_count'][0] == 5
        assert fresh_result['TOTAL|total_view_count'][8] == 'exact'
        # An inconsistent source does not freeze/replace the authoritative
        # native number, and cannot attach false exact/zero completeness.
        mismatch, proof = verified_source_quality(runtime, day, original, reference)
        assert mismatch['SKU:100|view_count'][0] == 3
        assert mismatch['SKU:100|view_count'][11:13] == ['unknown_scope', None]
        assert proof['sources']['seller_funnel_snapshot']['mismatched_count'] > 0
        # Wrong-day, unproven, malformed and absent operands never certify
        # metadata, including when an old rendered value is numeric or zero.
        intraday = deepcopy(observed)
        intraday[0]['source_fetched_at'] = day + 'T16:00:00+05:00'
        for replacement in (None, payloads(day, intraday)['seller_funnel_snapshot'], _to_namespace({'kind':'success','date':'2099-01-01','count':0,'items':[],
                                                 'source_fetched_at':'capture'}),
                            _to_namespace({'kind':'success','date':day,'count':2,'items':[]})):
            current['seller_funnel_snapshot'] = replacement
            unchanged, proof = verified_source_quality(runtime, day, original, reference)
            assert unchanged['SKU:100|view_count'] == original['SKU:100|view_count']
            assert 'seller_funnel_snapshot' not in proof['sources']
        assert retained_scope_reader(source, source.edition())('2099-01-01') is None
        assert verified_source_quality(runtime, day, original, None) == (original, None)
        assert before_files == {str(p): p.read_bytes() for p in source.root.rglob('*') if p.is_file()}
        dependency_proof(Path(raw)/'dependency')
    print('dated source quality smoke: metadata-only/native numbers/dated scope/zero/missing/fresh/mismatch/read-only passed')


if __name__ == '__main__':
    main()
