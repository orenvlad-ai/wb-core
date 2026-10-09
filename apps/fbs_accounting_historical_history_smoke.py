#!/usr/bin/env python3
"""LOCAL real native source/book/ready → compiler → immutable History objects.

The posting/stock fixture is synthetic. Its minimal operational metadata tables
are completed from real native DDL only in this temporary DB; every historical
quantity/capital operand already comes from its archived official generations,
posted native receipt, six saved stage payloads and accepted book. No Linux FD
capability is mocked and no completed operator/service recovery is claimed.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import sqlite3
import sys
import time
import unittest
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_accounting_historical_publication_smoke as publication_fixture
NOW=publication_fixture.NOW
from packages.application import fbs_accounting_historical_history as history
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_accounting_historical_publication as publication
from packages.application import ready_publication as ready
from packages.application.registry_upload_db_backed_runtime import _ensure_schema,RegistryUploadDbBackedRuntime,_deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter,update_live_history
from packages.application.web_vitrina_history_store import HistoryStore
from packages.application.fbs_snapshot_cost import close_candidate_period,canonical,fingerprint
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
from packages.application.owned_history_native_ack import consume_historical_history_ack


def complete_local_metadata(db):
    """Fixture-only bootstrap; never repairs or fills production source input."""
    with closing(sqlite3.connect(':memory:')) as schema,closing(sqlite3.connect(db)) as conn:
        schema.row_factory=conn.row_factory=sqlite3.Row
        _ensure_schema(schema)
        for table in schema.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            name=table[0]
            if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone():continue
            existing={r['name'] for r in conn.execute('PRAGMA table_info('+name+')')}
            for col in schema.execute('PRAGMA table_info('+name+')'):
                if col['name'] not in existing:
                    default=col['dflt_value'] if col['dflt_value'] is not None else ('0' if col['type'] in ('INTEGER','REAL') else "''")
                    conn.execute('ALTER TABLE '+name+' ADD COLUMN '+col['name']+' '+col['type']+' DEFAULT '+default)
        # Minimal fixture triggers predated the added metadata columns. Real
        # native bootstrap recreates the exact full-column source revision SQL.
        for trigger in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ready_input_%'").fetchall():conn.execute('DROP TRIGGER '+trigger[0])
        _ensure_schema(conn);conn.commit()


def cell(store,day,key):
    ref=store.edition()['days'][day]
    with closing(store._open_day(store.root/'objects'/(ref+'.sqlite3'))) as conn:
        return json.loads(zlib.decompress(conn.execute('SELECT payload FROM cells WHERE row_id=?',(key,)).fetchone()[0]))


class HistoricalHistory(unittest.TestCase):
    def setUp(self):
        self.fixture=publication_fixture.HistoricalPublication();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.publish();self.db=self.fixture.db
        complete_local_metadata(self.db)
        self.runtime=RegistryUploadDbBackedRuntime(self.fixture.runtime)
        from packages.application.own_product_capital import OwnProductCapitalBlock
        OwnProductCapitalBlock(runtime=self.runtime)
        self.keeper=sqlite3.connect(self.db);self.keeper.row_factory=sqlite3.Row;self.addCleanup(self.keeper.close)
        self.keeper.execute('PRAGMA journal_mode=WAL');ready.ensure_publication_schema(self.keeper);self.keeper.commit();self.keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
        self.private=TemporaryDirectory(prefix='historical-actual-history-');self.addCleanup(self.private.cleanup)
        self.root=Path(self.private.name)
        self.store=HistoryStore(self.root/'history')
        self.receipt=history.HistoricalReceipt(self.runtime,'local-late-publication','1')

    def adapter(self,now=NOW):
        return LiveNativeAdapter(db_path=self.db,runtime_dir=self.runtime.runtime_dir,cache_dir=self.root/'proofs',now=now,
            date_from='2026-09-08',date_to='2026-09-10',formula_epoch='local-historical-revision')

    def compile(self,now=NOW):
        adapter=self.adapter(now)
        result=update_live_history(adapter=adapter,runtime=self.runtime,store=self.store,max_recomputes=31,deadline_monotonic=time.monotonic()+60,
            rolling14=False,group_blocks=True,backfill_dates=list(self.receipt.dates))
        self.assertIn(result['status'],{'published','unchanged'})
        if result['status']=='published':self.assertTrue(result['compiler_constructed'])
        return adapter

    def test_actual_native_history_management_cost_and_physical_stage_are_distinct(self):
        before=self.keeper.execute('SELECT total_changes()').fetchone()[0]
        adapter=self.compile()
        proof=self.receipt.validate_native_readonly(adapter=adapter,store=self.store,now=NOW)
        self.assertEqual(set(proof['native']),{'2026-09-08','2026-09-09'})
        self.assertEqual(cell(self.store,'2026-09-08','SKU:1|own_capital_FF_capital_rub')[0],350000)
        self.assertEqual(cell(self.store,'2026-09-08','SKU:1|own_capital_PRODUCTION_TO_FF_capital_rub')[0],500)
        self.assertEqual(self.keeper.execute('SELECT total_changes()').fetchone()[0],before)
        self.assertEqual(accounting.load(self.runtime.runtime_dir,version=self.fixture.before)[0],self.fixture.book)

    def test_no_current_book_only_ack_and_no_forged_supervisor_proof(self):
        with self.assertRaises((ValueError,TypeError,KeyError)):self.receipt.validate_native_readonly(adapter=self.adapter(),store=self.store,now=NOW)
        with self.assertRaisesRegex(Exception,'not_supervised'):consume_historical_history_ack({},self.runtime.runtime_dir,self.receipt.manifest['manifest_digest'],self.receipt.dates)

    def test_old_edition_remains_after_new_exact_source_history_revision(self):
        self.compile();prior=self.store._current()['current'];paths={p:p.read_bytes() for p in (self.store.root/'objects').iterdir()}
        self.compile()
        self.assertEqual(self.store._current()['current'],prior)
        self.assertTrue(all(p.read_bytes()==value for p,value in paths.items()))

    def test_rollover_after_publication_requires_the_original_open_date(self):
        next_now=datetime.fromisoformat('2026-09-11T14:00:00+00:00')
        self.receipt.freeze_scope(next_now)
        self.assertEqual(self.receipt.dates,('2026-09-08','2026-09-09','2026-09-10'))
        with self.assertRaisesRegex(ValueError,'rollover_date_unpublished'):self.receipt.validate_sources_readonly(now=next_now)
        book,version=accounting.load(self.runtime.runtime_dir)
        book['state']=close_candidate_period(book['state'],'2026-09-10',today='2026-09-11')
        day='2026-09-10';book['shared_days'][day]=build_shared_cost_day(book['state'],book['wb_days'][day],day)
        book['presentations'][day]=FbsInventorySnapshot(fbs_state=book['state'],wb_capture=book['wb_days'][day],retained=book['retained_days'][day],day=day).payload()
        new=accounting._save_book(self.runtime.runtime_dir,book,expected=version,operation_id='LOCAL ordinary rollover close')
        with ready.readonly(self.db) as conn:
            expected=ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date=day)
            envelope=publication.materialize_exact_dates(_deserialize_sheet_vitrina_plan(expected.plan_json),book=book,version=new,runtime_dir=self.runtime.runtime_dir,
                target={'bundle_version':'fixture-v1','as_of_date':day},dates=[day],now=next_now,connection=conn)
            from packages.application.inventory_quantity import resolve_plan_quantities,CONTRACT
            q=resolve_plan_quantities(envelope,day=day,prepared_book=book,require_closed=True)
        from packages.application.sheet_vitrina_v1_inventory_history import append_inventory_history_capture,append_inventory_history_finalization
        with self.keeper:
            self.keeper.execute("BEGIN IMMEDIATE")
            ready.replace_ready(self.keeper,expected=expected,plan_json=_serialize_sheet_vitrina_plan(envelope),refreshed_at='2026-09-11T14:00:00Z')
            capture=append_inventory_history_capture(self.keeper,business_date=day,capture_kind='accepted_refresh',formula_version=CONTRACT,
                facility_roster=q['facility_roster'],source_manifest=q['source_manifest'],components=q['components'],captured_at='2026-09-11T14:00:00Z',
                bundle_version='fixture-v1',ready_snapshot_id=envelope.snapshot_id,ready_plan_version=envelope.plan_version)
            append_inventory_history_finalization(self.keeper,business_date=day,capture_id=capture['capture_id'],finalization_identity='LOCAL ordinary closed D',
                finalized_at='2026-09-11T14:00:00Z',provenance={'fixture':'actual ordinary append API'},expected_predecessor='')
        self.assertEqual(set(self.receipt.validate_sources_readonly(now=next_now)['dated']),set(self.receipt.dates))
        adapter=self.compile(next_now)
        proof=self.receipt.validate_native_readonly(adapter=adapter,store=self.store,now=next_now)
        self.assertIn(day,proof['native']);self.assertEqual(cell(self.store,day,'SKU:1|own_capital_FF_capital_rub')[0],350000)

    def test_actual_finance_sale_return_recalculates_from_revised_dated_book(self):
        from datetime import date
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.wb_finance_weekly_cost_cutover_smoke import _row,_week
        fixture=publication_fixture.HistoricalPublication();fixture.setUp();self.addCleanup(fixture.doCleanups)
        complete_local_metadata(fixture.db)
        block=WbFinanceWeeklyBlock(fixture.runtime,now_factory=lambda:NOW)
        before=block.ingest_week(date(2026,9,7),date(2026,9,13),[_row(701,'2026-09-08',nm_id=1,quantity=2),_row(702,'2026-09-09',nm_id=1,returned=True)])['aggregate']
        self.assertEqual(before['cogs'],'160.0000')
        # A separate unchanged forward week is retained as real derived state.
        block.ingest_week(date(2026,9,14),date(2026,9,20),[_row(703,'2026-09-15',nm_id=2)])
        other=_week(block,'2026-09-14')
        fixture.manifest=fixture.prepare();fixture.publish()
        receipt=block.recalculate_stale_cost_weeks()
        self.assertIn(receipt['status'],{'applied','already_current'})
        self.assertTrue(receipt['non_target_preserved'])
        self.assertEqual(receipt['post_verify_stale_week_count'],0)
        self.assertEqual(receipt['weeks'][0]['cogs'],'180.0000')
        self.assertEqual(_week(block,'2026-09-14'),other)

    def test_thirty_one_day_real_publication_and_native_history(self):
        now=datetime.fromisoformat('2026-10-08T14:00:00+00:00')
        fixture=publication_fixture.HistoricalPublication();fixture.fixture_now=now;fixture.setUp();self.addCleanup(fixture.doCleanups)
        published=fixture.publish();self.assertEqual(len(published['dates']),31)
        complete_local_metadata(fixture.db);runtime=RegistryUploadDbBackedRuntime(fixture.runtime)
        from packages.application.own_product_capital import OwnProductCapitalBlock
        OwnProductCapitalBlock(runtime=runtime)
        with closing(sqlite3.connect(fixture.db)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL');ready.ensure_publication_schema(keeper);keeper.commit()
            receipt=history.HistoricalReceipt(runtime,'local-late-publication','1')
            root=self.root/'thirty-one';store=HistoryStore(root/'history')
            adapter=LiveNativeAdapter(db_path=fixture.db,runtime_dir=runtime.runtime_dir,cache_dir=root/'proofs',now=now,
                date_from='2026-09-08',date_to='2026-10-08',formula_epoch='local-historical-thirty-one')
            result=update_live_history(adapter=adapter,runtime=runtime,store=store,max_recomputes=31,deadline_monotonic=time.monotonic()+60,
                rolling14=False,group_blocks=True,backfill_dates=list(receipt.dates))
            self.assertEqual(result['status'],'published')
            proof=receipt.validate_native_readonly(adapter=adapter,store=store,now=now)
            self.assertEqual(len(proof['native']),30)
            for day in receipt.dates:
                self.assertEqual(cell(store,day,'SKU:1|own_capital_FF_capital_rub')[0],350000)
                self.assertEqual(cell(store,day,'SKU:1|own_capital_PRODUCTION_TO_FF_capital_rub')[0],500)
            self.assertEqual(accounting.load(fixture.runtime,version=fixture.before),(fixture.book,fixture.before))

    def test_actual_service_and_functional_queue_cannot_bypass_history_completion(self):
        fixture,runtime=self.service_fixture()
        from apps.warehouse_functional_runner import _recalculate_downstream_finance_cost
        from packages.application import operator_warehouse_documents as operations
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        with warehouse_functional_job_lock(runtime.runtime_dir):
            finance=_recalculate_downstream_finance_cost(runtime)
            result=operations.reconcile(runtime,request_ids=['native:new-late-receipt'],finance_receipt=finance,
                economics_receipt=accounting.current_publication_receipt(runtime,now=fixture.now),now=fixture.now)
        self.assertEqual(result['processed_count'],0)
        with ready.readonly(fixture.db) as conn:
            source=conn.execute('SELECT state,recovery_operation_id FROM sheet_vitrina_v1_ff_pool_document_requests WHERE request_id=?',('native:new-late-receipt',)).fetchone()
            self.assertEqual(source['state'],'posted')
            self.assertEqual(conn.execute('SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?',(source['recovery_operation_id'],)).fetchone()[0],'mutation_running')

    def service_fixture(self,end='2026-09-10'):
        from apps import operator_warehouse_documents_smoke as native_queue
        from unittest.mock import patch
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        fixture=publication_fixture.HistoricalPublication();fixture.fixture_now=datetime.fromisoformat(end+'T14:00:00+00:00');fixture.service_receipt=True
        fixture.setUp();self.addCleanup(fixture.doCleanups);fixture.publish()
        complete_local_metadata(fixture.db);runtime=RegistryUploadDbBackedRuntime(fixture.runtime)
        from packages.application.own_product_capital import OwnProductCapitalBlock
        OwnProductCapitalBlock(runtime=runtime)
        witness=native_queue.OperatorDocuments();witness.runtime=runtime;witness.root=fixture.runtime
        # Only synthetic native source data/date varies. The real serialized
        # WarehouseFunctionalBlock.apply_plan and queue ack run unchanged.
        with patch.object(native_queue,'DAY',end),patch.object(native_queue,'NOW',fixture.now),patch.object(native_queue,'STAMP',fixture.now.isoformat()):
            with warehouse_functional_job_lock(runtime.runtime_dir):witness.functional_publish()
        return fixture,runtime

    @unittest.skipUnless(sys.platform=='linux','genuine inherited FD authority requires Linux')
    def test_genuine_owned_history_then_native_service_retention_completes(self):
        from apps.owned_history_worker_smoke import ownership
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application.fbs_accounting_historical_cycle import finalize
        from packages.application.operator_warehouse_documents import read_acceptance
        import hashlib
        fixture,runtime=self.service_fixture(end='2026-10-08')
        receipt=history.HistoricalReceipt(runtime,'local-late-publication','1');root=self.root/'owned-real'
        with closing(sqlite3.connect(fixture.db)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL');ready.ensure_publication_schema(keeper);keeper.commit();keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
            # The production mount admission is the standard LOCAL test seam;
            # kernel inherited descriptors, owner/capability, actual fixed
            # child, source proof and acknowledgment are never mocked.
            with heavy_admitted(runtime.runtime_dir,operation='cycle'):
                with ownership(runtime,root,seconds=120) as worker:
                    contract=json.loads(worker.config.runtime_contract.read_text())
                    repo=Path(__file__).resolve().parents[1]
                    contract['formula_code_hashes']={p:hashlib.sha256((repo/p).read_bytes()).hexdigest() for p in contract['formula_code_hashes']}
                    contract['formula_epoch']='wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(contract['formula_code_hashes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    worker.config.formula_epoch=contract['formula_epoch'];worker.config.runtime_contract.write_text(json.dumps(contract))
                    result=worker.complete(fixture.now,backfill_dates=tuple(d for d in receipt.dates if d<'2026-10-07'),historical_receipt=receipt,total_seconds=240)
                    self.assertTrue(result['historical_ack']);config=worker.config
                # Context exit reaps the actual child and releases History
                # locks; reacquire a genuine warehouse owner before finalize.
                with warehouse_functional_job_lock(runtime.runtime_dir):completed=finalize(runtime,config=config,now=fixture.now)
            self.assertEqual(completed['status'],'complete')
            accepted=read_acceptance(fixture.db,'native:new-late-receipt')
            self.assertEqual(accepted['state'],'completed')
            self.assertEqual(accepted['processing_receipt']['native_recovery']['lifecycle_state'],'retained')
            self.assertTrue(accepted['processing_receipt']['publication_verified'])
            self.assertEqual(len(accepted['processing_receipt']['historical_publication']['history_ack']['native']),30)
            self.assertIsNone(history.pending_receipt(runtime))


if __name__=='__main__':unittest.main()
