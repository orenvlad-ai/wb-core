"""Real exact-day proof -> routine plan -> web/native numeric cells with warnings."""
from __future__ import annotations
from contextlib import ExitStack, closing
from datetime import datetime
import json
import os
import shutil
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps.promo_archive_publication_smoke import _ready, _seed_reconstruction_fixture
from apps.promo_archive_publication import _candidate
from apps.sheet_vitrina_v1_promo_live_source_smoke import _write_promo_run_fixture, _build_entrypoint, _MutableNowFactory
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.promo_campaign_archive import sync_promo_campaign_archive
from packages.application.promo_historical_recovery import recover_promo_display, qualified_reconstruction
from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.sheet_vitrina_v1_live_plan import _is_exact_snapshot_payload
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.web_vitrina_compact_table import CELL_FIELDS
DAY = '2026-05-03'


def seed(root):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    bundle = json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
    assert runtime.ingest_bundle(bundle, activated_at='2026-05-03T08:00:00Z').status == 'accepted'
    ids = [int(x['nm_id']) for x in bundle['config_v2'] if x['enabled']]
    _write_promo_run_fixture(runtime_dir=root, run_name=DAY+'__fixture', promo_folder='2400__2300__promo',
        promo_id=2400, period_id=2300, promo_title='Promo', promo_period_text='03 мая 02:00 -> 03 мая 23:59',
        promo_start_at=DAY+'T02:00', promo_end_at=DAY+'T23:59', workbook_rows=[{'nm_id':ids[0], 'plan_price':508.0}])
    sync_promo_campaign_archive(root)
    runtime.save_temporal_source_slot_snapshot(source_key='prices_snapshot', snapshot_date=DAY,
        snapshot_role='accepted_current_snapshot', captured_at=DAY+'T08:00:00Z',
        payload=SimpleNamespace(kind='success', snapshot_date=DAY, items=[SimpleNamespace(nm_id=x,price_seller=508.0,price_seller_discounted=508.0) for x in ids]))
    with sqlite3.connect(str(runtime.db_path)) as conn:
        conn.execute("INSERT INTO sheet_vitrina_v1_sku_groups(group_key,label,is_active,created_at,updated_at) VALUES('fixture','Fixture',1,?,?)",(DAY,DAY))
        conn.executemany("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,is_active,our_sku,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES(?,1,?,?,?,'fixture',?,'[]',?,?)", [(str(x),str(x),x,str(x),str(x),DAY,DAY) for x in ids])
    _ready(root, ids)
    _seed_reconstruction_fixture(root, ids)
    # Old metadata-only announcement cannot be dismissed by a later failed identity.
    original = json.loads((root/'promo_campaign_archive/2400__2300__promo/archive_record.json').read_text())
    announcement = root/'promo_campaign_archive/2901__pending__announcement'; announcement.mkdir()
    original.update(archive_key=announcement.name, archive_dir=str(announcement), workbook_present=False,
                    workbook_path=None, workbook_fingerprint=None, workbook_inspection_path=None,
                    downloaded_at=None, collected_at='2026-05-02T08:00:00+05:00')
    original['metadata'].update(promo_id=2901, period_id=None, promo_title='Announcement', promo_status='Акция запланирована. Список товаров появится ближе к старту акции',
        ui_status='future', ui_status_confidence='high', download_action_state='absent', status_evidence_sources=['footer_label'],
        collected_at='2026-05-02T08:00:00+05:00', temporal_classification='future', saved_path=None,
        campaign_identity_match=True, ui_loaded_success=True)
    (announcement/'archive_record.json').write_text(json.dumps(original))
    (announcement/'metadata.json').write_text(json.dumps(original['metadata']))
    later = root/'promo_xlsx_collector_runs'/ (DAY+'__zz_later'); later.mkdir()
    (later/'run_summary.json').write_text(json.dumps({'run_dir':str(later),'status':'partial','started_at':DAY+'T22:00:00+05:00',
        'timeline_candidates_found':2,'card_confirmed_count':1,'blocked_before_card_count':1,
        'hydration_attempts':[{'hydrated_success':True,'timeline_count':2}],
        'promos':[{'promo_id':None,'timeline_block_index':1}]}))
    return runtime, ids


def footprint(runtime):
    with sqlite3.connect(f'file:{runtime.db_path}?mode=ro',uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        return list(conn.execute("SELECT * FROM temporal_source_slot_snapshots ORDER BY source_key,snapshot_date,snapshot_role")), list(conn.execute("SELECT * FROM temporal_source_snapshots ORDER BY source_key,snapshot_date"))


def recover(runtime, ids):
    return recover_promo_display(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path, snapshot_date=DAY, requested_nm_ids=ids)


def assert_missing_run_archive(runtime, ids):
    from apps.production_apply_contract import AdapterError
    from packages.application.promo_historical_recovery import PromoHistoricalRecoveryError
    run=runtime.runtime_dir/'promo_xlsx_collector_runs'/(DAY+'__fixture')
    summary_path=run/'run_summary.json'; original=summary_path.read_bytes()
    missing=run/'promos/2500__2501__missing'; missing.mkdir()
    raw=json.loads((run/'promos/2400__2300__promo/metadata.json').read_text())
    raw.update(promo_id=2500,period_id=2501,promo_title='Missing current campaign')
    (missing/'metadata.json').write_text(json.dumps(raw))
    for status in ('downloaded','reused_archive','blocked_before_download'):
        summary=json.loads(original); summary.update(timeline_candidates_found=2,card_confirmed_count=2)
        summary['hydration_attempts'][0]['timeline_count']=2
        summary['promos'].append({'promo_id':2500,'timeline_block_index':1,'promo_title':'Missing current campaign',
            'status':status,'metadata_path':str(missing/'metadata.json'),'saved_path':str(missing/'workbook.xlsx'),
            'metadata':raw})
        summary_path.write_text(json.dumps(summary))
        assert recover(runtime,ids) is None
        try:
            _candidate(runtime.runtime_dir,[DAY],{DAY:{'identity_run':DAY+'__fixture','price_checkpoint_id':'crcp_fixture'}})
        except AdapterError as exc:
            assert 'run-archive-roster-mismatch' in str(exc) or 'run-material-incomplete' in str(exc),exc
        else: raise AssertionError('publication accepted a missing full campaign archive')
        try: _candidate(runtime.runtime_dir,[DAY],{DAY:'auto'})
        except AdapterError: pass
        else: raise AssertionError('auto publication accepted a missing full campaign archive')
    summary_path.write_bytes(original); shutil.rmtree(missing)
    assert recover(runtime,ids) is not None


def assert_downloaded_window(runtime, ids):
    from apps.production_apply_contract import AdapterError
    run=runtime.runtime_dir/'promo_xlsx_collector_runs'/(DAY+'__fixture')
    summary_path=run/'run_summary.json'; summary_original=summary_path.read_bytes()
    raw_path=run/'promos/2400__2300__promo/metadata.json'; raw_original=raw_path.read_bytes()
    workbook=raw_path.parent/'workbook.xlsx'; workbook_original=workbook.read_bytes(); old_time=workbook.stat().st_mtime
    stamp=datetime.fromisoformat(DAY+'T09:10:00+05:00').timestamp()
    os.utime(workbook,(stamp,stamp))
    summary=json.loads(summary_original); summary['finished_at']=DAY+'T09:20:00+05:00'
    summary['promos'][0].update(status='downloaded',saved_path=str(workbook))
    raw=json.loads(raw_original); raw['collected_at']=DAY+'T09:10:01+05:00'
    raw_path.write_text(json.dumps(raw)); summary_path.write_text(json.dumps(summary))
    result=recover(runtime,ids)
    assert result and result.diagnostics['historical_reconstruction']['material_observed_at_max']==DAY+'T04:10:01+00:00'
    assert result.diagnostics['historical_reconstruction']['identity_window_finished_at']==DAY+'T09:20:00+05:00'
    assert _candidate(runtime.runtime_dir,[DAY],{DAY:'auto'})['results'][DAY]['observation_quality']=='historical_composite_observation_only'
    for invalid in (None, DAY+'T09:05:00+05:00','2026-05-04T09:20:00+05:00',DAY+'T09:20:00'):
        summary['finished_at']=invalid; summary_path.write_text(json.dumps(summary))
        assert recover(runtime,ids) is None
        try: _candidate(runtime.runtime_dir,[DAY],{DAY:'auto'})
        except AdapterError: pass
        else: raise AssertionError('publication accepted an unqualified download window')
        try: _candidate(runtime.runtime_dir,[DAY],{DAY:{'identity_run':DAY+'__fixture','price_checkpoint_id':'crcp_fixture'}})
        except AdapterError: pass
        else: raise AssertionError('explicit publication accepted an unqualified download window')

    summary['finished_at']=DAY+'T09:20:00+05:00';summary_path.write_text(json.dumps(summary))
    raw['collected_at']=DAY+'T09:30:00+05:00';raw_path.write_text(json.dumps(raw))
    assert recover(runtime,ids) is None
    raw['collected_at']=DAY+'T09:10:01+05:00';raw_path.write_text(json.dumps(raw))
    workbook.write_bytes(workbook_original+b'tamper');os.utime(workbook,(stamp,stamp))
    assert recover(runtime,ids) is None
    workbook.write_bytes(workbook_original);os.utime(workbook,(old_time,old_time))
    raw_path.write_bytes(raw_original);summary_path.write_bytes(summary_original)
    assert recover(runtime,ids) is not None


def main():
    with TemporaryDirectory(prefix='promo-recovery-') as tmp:
        runtime, ids = seed(Path(tmp)/'runtime')
        assert_missing_run_archive(runtime, ids)
        assert_downloaded_window(runtime, ids)
        before = footprint(runtime)
        with patch('packages.application.promo_campaign_archive.sync_promo_campaign_archive', side_effect=AssertionError('no sync')):
            composite = recover(runtime, ids)
        assert composite and composite.kind == 'incomplete' and len(composite.items) == len(ids), composite
        assert {item.nm_id:item.promo_count_by_price for item in composite.items} == {x:1 if x==ids[0] else 0 for x in ids}
        assert composite.observation_quality == 'historical_composite_observation_only'
        assert not _is_exact_snapshot_payload(composite, DAY)
        assert footprint(runtime) == before
        proof = composite.diagnostics['historical_reconstruction']
        assert proof['later_run_count_not_used'] == 1 and proof['latest_later_attempt']['unresolved_identity_count'] == 1
        # Explicit publication selects the same operands, and overlays warning cells.
        candidate = _candidate(runtime.runtime_dir,[DAY],{DAY:'auto'})
        assert candidate['reconstruction_proof'][DAY] == proof
        published = candidate['results'][DAY]
        assert published['kind'] == 'success' and not _is_exact_snapshot_payload(SimpleNamespace(**published), DAY)
        from apps.promo_archive_publication import PromoArchivePublicationAdapter
        from apps.production_apply_launcher import execute
        request={'runtime_dir':str(runtime.runtime_dir),'dates':[DAY],'reconstruction':{DAY:'auto'}}
        adapter=PromoArchivePublicationAdapter()
        preview=adapter.preview(request,'fixture-composite-publication')
        receipt=execute(action='apply',adapter_name='promo_archive_publication_v1',operation_id='fixture-composite-publication',
            request=request,expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        assert receipt['state']=='applied' and adapter.readback(request,'fixture-composite-publication')['state']=='applied'
        assert runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DAY).metadata['server_cell_presentation']
        accepted_before = footprint(runtime)
        block = PromoLiveSourceBlock(runtime_dir=runtime.runtime_dir,now_factory=lambda: datetime.fromisoformat('2099-05-04T12:00:00+05:00'))
        entry = _build_entrypoint(runtime=runtime,promo_source_block=block,now_factory=_MutableNowFactory('2026-05-04T12:00:00+05:00'))
        status, payload = entry.sheet_plan_block._capture_promo_closed_day_from_cache(source_key='promo_by_price',temporal_slot='yesterday_closed',
            temporal_policy='exact_date',column_date=DAY,requested_nm_ids=ids)
        assert status.kind == 'incomplete' and payload and payload.items and not _is_exact_snapshot_payload(payload,DAY)
        assert footprint(runtime) == accepted_before  # Explicit composite never promoted by refresh.
        current_status,current_payload = entry.sheet_plan_block._capture_temporal_source_with_acceptance(
            source_key='promo_by_price',temporal_slot='today_current',temporal_policy='exact_date',
            column_date=DAY,requested_nm_ids=ids,loader=lambda:block.execute(SimpleNamespace(snapshot_date=DAY,nm_ids=ids)).result,
            execution_mode='auto_daily',accepted_role='accepted_current_snapshot',allow_persisted_retry=False,current_web_source_sync_note=None)
        assert current_status.kind=='incomplete' and current_payload and current_payload.items
        assert not _is_exact_snapshot_payload(current_payload,DAY) and footprint(runtime)==accepted_before

        # Real plan and read surfaces; other collectors are deliberately isolated.
        result = entry._run_sheet_refresh(as_of_date=DAY,log=None,execution_mode='auto_daily')
        assert result['status'] in {'success','warning'}, result
        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DAY)
        promo_rows = [row for row in plan.sheets[0].rows if len(row)>1 and str(row[1]).endswith('|promo_count_by_price')]
        assert promo_rows and all(isinstance(row[2],(int,float)) for row in promo_rows), promo_rows
        assert 'closed_day_freshness_unproven=true' in next(row for sheet in plan.sheets if sheet.sheet_name=='STATUS' for row in sheet.rows if row[0]=='promo_by_price[yesterday_closed]')[-1]
        # Same compiler as native publication, with pinned SQLite read authority.
        with window_read_context(runtime.db_path,runtime_dir=runtime.runtime_dir):
            compiler = NativeDatedCompiler(runtime,datetime.fromisoformat('2026-05-04T12:00:00+05:00'),DAY,DAY,group_blocks=True)
            dated = compiler.compile(DAY)
        warned = []
        for key, packed in dated['cells'].items():
            if key.rsplit('|',1)[-1] in {'promo_count_by_price','total_promo_count_by_price'}:
                cell = dict(zip(CELL_FIELDS, packed))
                assert isinstance(cell['value'],(int,float)), (key,cell)
                assert cell['presentation_state']=='unconfirmed' and cell['quality_state']=='preliminary', (key,cell)
                assert 'Полнота на конец дня не подтверждена' in cell['quality_reason'], (key,cell)
                warned.append(key.split(':',1)[0].split('|',1)[0])
        assert {'SKU','TOTAL','GROUP'}.issubset(warned), warned
        # Publication's prior fixture intentionally has a role-only alias date;
        # remove it before exercising actual calendar history availability.
        with sqlite3.connect(str(runtime.db_path)) as conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-05-02'")
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
        from packages.application.web_vitrina_history_store import HistoryStore
        contract=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
        with ExitStack() as keepers:
            for path in (runtime.db_path,runtime.runtime_dir/'fbs-snapshot-accounting.sqlite3'):
                if path.exists():
                    conn=keepers.enter_context(closing(sqlite3.connect(path)))
                    conn.execute('PRAGMA journal_mode=WAL'); conn.execute('SELECT 1 FROM sqlite_master').fetchone()
            store=HistoryStore(Path(tmp)/'native-history')
            old_adapter=LiveNativeAdapter(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,cache_dir=Path(tmp)/'old-proofs',
                now=datetime.fromisoformat('2026-05-04T12:00:00+05:00'),date_from='2026-05-01',date_to='2026-05-04',formula_epoch='prior-fixture-epoch')
            baseline=update_live_history(adapter=old_adapter,runtime=runtime,store=store,rolling14=True,max_recomputes=4,group_blocks=True)
            assert baseline['status']=='published',baseline
            old_edition=store.edition()
            new_adapter=LiveNativeAdapter(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,cache_dir=Path(tmp)/'new-proofs',
                now=datetime.fromisoformat('2026-05-04T12:00:00+05:00'),date_from=DAY,date_to='2026-05-04',formula_epoch=contract['formula_epoch'])
            bounded=update_live_history(adapter=new_adapter,runtime=runtime,store=store,rolling14=True,max_recomputes=2,
                backfill_dates=[DAY,'2026-05-04'],group_blocks=True)
            assert bounded['status']=='published' and bounded['recomputes']==2,bounded
            new_edition=store.edition()
            assert {d:new_edition['days'][d] for d in ('2026-05-01','2026-05-02')}=={d:old_edition['days'][d] for d in ('2026-05-01','2026-05-02')}

        # Intentionally corrupt only this disposable DB fixture; production
        # canonical checkpoints remain immutable and recovery stays query-only.
        with sqlite3.connect(str(runtime.db_path)) as conn:
            conn.execute('DROP TRIGGER change_registry_observation_values_no_update')
            conn.execute('DROP TRIGGER change_registry_checkpoint_source_manifests_no_update')
        # Wrong-day checkpoint, incomplete roster, true file loss stay missing.
        with sqlite3.connect(str(runtime.db_path)) as conn:
            conn.execute("UPDATE change_registry_observation_values SET observed_at='2026-05-02T03:00:00Z'")
        assert recover(runtime,ids) is None
        with sqlite3.connect(str(runtime.db_path)) as conn:
            conn.execute("UPDATE change_registry_observation_values SET observed_at='2026-05-03T03:00:00Z'")
            conn.execute("UPDATE change_registry_checkpoint_source_manifests SET observed_count=0 WHERE source_name='prices'")
        assert recover(runtime,ids) is None
        with sqlite3.connect(str(runtime.db_path)) as conn:
            conn.execute("UPDATE change_registry_checkpoint_source_manifests SET observed_count=expected_count WHERE source_name='prices'")
        archive = runtime.runtime_dir/'promo_campaign_archive/2400__2300__promo'
        workbook = archive/'workbook.xlsx'; workbook.unlink()
        assert recover(runtime,ids) is not None  # Canonical validated normalized archive is enough.
        with patch('packages.application.promo_historical_recovery.MAX_RUNS',1):
            assert recover(runtime,ids) is None
        with patch('packages.application.promo_historical_recovery.MAX_QUERIES',0):
            assert recover(runtime,ids) is None
        with patch('packages.application.promo_historical_recovery.MAX_BYTES',1):
            assert recover(runtime,ids) is None
        with patch('packages.application.promo_historical_recovery.MAX_SECONDS',-1):
            assert recover(runtime,ids) is None
        with patch('packages.application.promo_historical_recovery.MAX_ATTEMPTS',0):
            assert recover(runtime,ids) is None
        with patch('packages.application.promo_historical_recovery.MAX_FILES',1):
            assert recover(runtime,ids) is None
        with patch('packages.application.promo_historical_recovery.MAX_ARCHIVE_RECORDS',1):
            assert recover(runtime,ids) is None
        assert recover(runtime,ids) is not None  # Negative limits did not damage proof.
        manifest = archive/'campaign_rows_manifest.json'; saved=manifest.read_bytes(); manifest.write_text('{}')
        assert recover(runtime,ids) is None  # Real workbook + canonical rows lost/corrupt.
        (runtime.runtime_dir/'promo_xlsx_collector_runs'/(DAY+'__fixture')/'promos/2400__2300__promo/workbook.xlsx').unlink()
        entry._run_sheet_refresh(as_of_date=DAY,log=None,execution_mode='auto_daily')
        blank_plan=runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DAY)
        assert all(row[2] in ('',None) for row in blank_plan.sheets[0].rows
                   if len(row)>1 and str(row[1]).endswith('|promo_count_by_price'))

        manifest.write_bytes(saved)
        print('promo historical recovery smoke passed: proof/read-only/next-refresh/web/native SKU TOTAL GROUP warning/negative/bounded')

if __name__=='__main__': main()
