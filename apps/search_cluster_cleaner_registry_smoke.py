#!/usr/bin/env python3
"""Populated legacy migration, rollback, exact query items/facts and evidence."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from dataclasses import replace
import json
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import os
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.change_registry import ensure_change_registry_schema,ChangeRegistryRepository,target_identity,canonical_digest
from packages.application.change_registry_search_cluster import prepare_in_transaction,confirm_in_transaction,needs_bidirectional_upgrade,bidirectional_ready
import packages.application.change_registry as registry_module
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime,_ensure_schema
from packages.application.warehouse_functional import WarehouseFunctionalBlock
from packages.contracts.search_cluster_cleaner import Account,Target
from packages.application.storage_registry import StoreRegistry
from apps.search_cluster_cleaner_registry_upgrade import plan as upgrade_plan,write_journal,main as upgrade_main

LEGACY=Path(__file__).parent/'fixtures/search_cluster_cleaner_legacy_registry.json'
def legacy_sql():
    fixture=json.loads(LEGACY.read_text(encoding='utf-8'))
    assert fixture['schema']=='wb-core.synthetic-legacy-registry/v1'
    sql=fixture['sql']
    assert isinstance(sql,str) and hashlib.sha256(sql.encode('utf-8')).hexdigest()==fixture['sql_sha256']
    return sql


NOW='2026-09-11T00:00:00+00:00';AFTER='2026-09-11T00:01:00+00:00'
ORPHAN_FINANCE_VIEW='finance_raw_current_rows'
ORPHAN_FINANCE_VIEW_SQL='CREATE VIEW finance_raw_current_rows AS SELECT * FROM finance_raw_batch_rows'


def add_orphan_finance_view(conn):
    conn.execute(ORPHAN_FINANCE_VIEW_SQL)
    return conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?",(ORPHAN_FINANCE_VIEW,)).fetchone()[0]


def snapshot(conn):
    result={}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'change_registry_%' ORDER BY name").fetchall():
        columns=[r[1] for r in conn.execute('PRAGMA table_info('+table+')')]
        result[table]=(columns,[tuple(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')])
    return result


def registry_objects(conn):
    return [tuple(row) for row in conn.execute("""SELECT type,name,tbl_name,sql
        FROM sqlite_master WHERE name LIKE 'change_registry_%' ORDER BY type,name""")]


def pragma_settings(conn):
    return tuple(
        conn.execute(f'PRAGMA {name}').fetchone()[0]
        for name in ('foreign_keys', 'legacy_alter_table')
    )


class FailingConnection(sqlite3.Connection):
    fail=False
    def execute(self,sql,*args,**kwargs):
        if self.fail and sql.startswith('INSERT INTO change_registry_facts_new'):raise sqlite3.OperationalError('synthetic migration copy failure')
        return super().execute(sql,*args,**kwargs)


class RegistryTests(unittest.TestCase):
    @staticmethod
    def populated_old_direction(conn):
        original=registry_module._field_value_check
        def old_check(prefix,*,requested):
            return original(prefix,requested=requested).replace(f'{prefix}_integer IN (0,1)',f'{prefix}_integer={int(requested)}')
        with patch.object(registry_module,'_field_value_check',old_check):ensure_change_registry_schema(conn)
        account=Account('synthetic-seller','ads');target=Target(11,101,contract_verified=True)
        conn.execute('BEGIN IMMEDIATE')
        # Old CHECK can be populated with a historical generic item/fact by
        # temporarily suppressing only the new upgrade preflight in the test.
        with patch('packages.application.change_registry_search_cluster.needs_bidirectional_upgrade',return_value=False):
            prepare_in_transaction(conn,operation_id='old-addition',account=account,target=target,queries=['old exact'],
                                   created_at=NOW,before_at=NOW,provenance={'fixture':True})
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        confirm_in_transaction(conn,operation_id='old-addition',present_queries=['old exact'],observed_at=AFTER,
                               before_at=NOW,evidence={'minus':['old exact']})
        conn.commit()

    def test_bidirectional_upgrade_is_explicit_and_preserves_populated_registry(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
        self.populated_old_direction(conn)
        before=snapshot(conn)
        self.assertTrue(needs_bidirectional_upgrade(conn))
        ensure_change_registry_schema(conn);self.assertTrue(needs_bidirectional_upgrade(conn))
        ensure_change_registry_schema(conn,upgrade_bidirectional=True)
        self.assertFalse(needs_bidirectional_upgrade(conn))
        for table,(columns,rows) in before.items():
            self.assertEqual([tuple(r) for r in conn.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY rowid')],rows)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE change_registry_items SET before_value_integer=1 WHERE operation_id='old-addition'")
        conn.rollback()
        account=Account('synthetic-seller','ads');target=Target(11,101,contract_verified=True)
        conn.execute('BEGIN IMMEDIATE')
        prepare_in_transaction(conn,operation_id='new-return',account=account,target=target,queries=[],returns=['old exact'],
                               created_at=AFTER,before_at=NOW,provenance={'fixture':True})
        confirm_in_transaction(conn,operation_id='new-return',present_queries=[],observed_at=AFTER,
                               before_at=NOW,evidence={'minus':[]})
        conn.commit()
        fact=conn.execute("SELECT before_value_integer,after_value_integer FROM change_registry_facts WHERE query_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(fact),(1,0))
        conn.close()

    def test_old_schema_pauses_manual_intake_and_durable_worker_until_reviewed_upgrade(self):
        from apps.search_cluster_cleaner_write_fixture import fixture,OWNER
        from packages.application.search_cluster_cleaner_web import CleanerWeb
        from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
        from packages.application.search_cluster_cleaner_self_service import ManualCleanerCoordinator
        from packages.contracts.search_cluster_cleaner import CleanerError
        original=registry_module._field_value_check
        def old_check(prefix,*,requested):
            return original(prefix,requested=requested).replace(f'{prefix}_integer IN (0,1)',f'{prefix}_integer={int(requested)}')
        with patch.object(registry_module,'_field_value_check',old_check):
            with fixture() as f:
                # Only fixture creation uses the old CHECK. The candidate
                # migration itself must use the current reviewed DDL.
                registry_module._field_value_check=original
                web=CleanerWeb(f.app,generation='g1');web.worker_status=lambda:'ready'
                with f.store.read() as c:self.assertFalse(bidirectional_ready(c))
                self.assertFalse(web.summary(OWNER)['registry_ready'])
                self.assertEqual(web.batch_eligibility(OWNER)['error'],'registry_upgrade_required')
                for command in (lambda:web.start_manual_clean(dict(request_id='old-schema-single-0001',advert_id=11,nm_id=101),OWNER),
                                lambda:web.start_manual_batch(dict(request_id='old-schema-batch-0001',selected_categories=['active'],targets=[dict(advert_id=11,nm_id=101)]),OWNER)):
                    with self.assertRaises(CleanerError) as blocked:self.assertIsNone(command())
                    self.assertEqual(blocked.exception.code,'registry_upgrade_required')
                with f.store.transaction() as c:
                    f.app._event(c,'self_service_requested',dict(job_id='paused-manual-job-0001'))
                    f.app._event(c,'self_service_batch_requested',dict(batch_id='paused-manual-batch-0001'))
                child=ManualCleanerCoordinator(f.app,object())
                parent=BatchCleanerCoordinator(f.app,generation='g1')
                self.assertEqual(child.pending_jobs(),[])
                self.assertEqual(parent.pending_batches(),[])
                self.assertEqual(f.fake.writes,[])
                registry=f.store.registry
                with registry.session('operational',mode='ro',operation='old_schema_gate_plan') as c:
                    reviewed=upgrade_plan(c,registry)
                journal=Path(f.root)/'registry-upgrade-rollback.json'
                upgrade_main(['--runtime-dir',str(registry.runtime_dir),'--apply',
                              '--expected-plan-sha256',reviewed['plan_sha256'],'--journal-path',str(journal)])
                self.assertTrue(web.summary(OWNER)['registry_ready'])
                self.assertEqual(child.pending_jobs(),['paused-manual-job-0001'])
                self.assertEqual(parent.pending_batches(),['paused-manual-batch-0001'])
                self.assertEqual(f.fake.writes,[])

    def test_bidirectional_upgrade_copy_failure_rolls_back(self):
        conn=sqlite3.connect(':memory:',factory=FailingConnection);conn.row_factory=sqlite3.Row
        self.populated_old_direction(conn)
        before=conn.serialize();conn.fail=True
        with self.assertRaisesRegex(sqlite3.OperationalError,'synthetic migration copy failure'):
            ensure_change_registry_schema(conn,upgrade_bidirectional=True)
        self.assertEqual(conn.serialize(),before)
        self.assertTrue(needs_bidirectional_upgrade(conn))
        conn.close()

    def test_offline_plan_journal_exact_apply_and_wrong_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=StoreRegistry(Path(tmp));db=registry.resolve('operational')
            conn=sqlite3.connect(db);conn.row_factory=sqlite3.Row
            self.populated_old_direction(conn);conn.close()
            with registry.session('operational',mode='ro',operation='upgrade_test_plan') as c:
                before=upgrade_plan(c,registry)
                current=registry.load()
                switched=replace(current,manifest_sha256='sha256:'+'f'*64)
                with patch.object(registry,'load',side_effect=[current,switched]):
                    with self.assertRaisesRegex(RuntimeError,'generation changed during registry plan'):
                        upgrade_plan(c,registry)
            self.assertTrue(before['needed'])
            self.assertEqual([row['rows'] for row in before['tables']],[1,1])
            journal=Path(tmp)/'approved-journal.json'
            journal_sha=write_journal(journal,registry,before)
            self.assertEqual(hashlib.sha256(journal.read_bytes()).hexdigest(),journal_sha)
            self.assertEqual(os.stat(journal).st_mode & 0o777,0o600)
            contents=json.loads(journal.read_text())
            self.assertEqual(set(contents['tables']),{'change_registry_items','change_registry_facts'})
            self.assertTrue(all(value['create_table_sql'].startswith('CREATE TABLE') for value in contents['tables'].values()))
            other=sqlite3.connect(Path(tmp)/'wrong.sqlite3')
            with self.assertRaisesRegex(RuntimeError,'open database does not match'):
                upgrade_plan(other,registry)
            other.close()
            with self.assertRaisesRegex(RuntimeError,'reviewed registry plan changed'):
                upgrade_main(['--runtime-dir',tmp,'--apply','--expected-plan-sha256','0'*64,
                              '--journal-path',str(Path(tmp)/'wrong-journal.json')])
            with registry.session('operational',mode='ro',operation='upgrade_test_unchanged') as c:
                self.assertEqual(upgrade_plan(c,registry)['plan_sha256'],before['plan_sha256'])
            # CLI requires its own exclusive journal path, preserving the
            # previously reviewed artifact as immutable evidence.
            self.assertEqual(upgrade_main(['--runtime-dir',tmp,'--apply','--expected-plan-sha256',before['plan_sha256'],
                                           '--journal-path',str(Path(tmp)/'apply-journal.json')]),0)
            with registry.session('operational',mode='ro',operation='upgrade_test_after') as c:
                after=upgrade_plan(c,registry)
                self.assertFalse(after['needed'])
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual([row['row_sha256'] for row in after['tables']],
                                 [row['row_sha256'] for row in before['tables']])
                self.assertEqual(after['schema_objects_sha256'],before['schema_objects_sha256'])

    def test_reviewed_upgrade_rejects_intervening_registry_row_without_rebuild(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        self.populated_old_direction(conn)
        objects=[tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN('trigger','index') AND sql IS NOT NULL ORDER BY type,name")]
        from packages.application.change_registry_search_cluster import _row_digest
        plan=dict(tables=[dict(name=table,rows=_row_digest(conn,table)[0],row_sha256=_row_digest(conn,table)[1],
                               ddl_sha256=hashlib.sha256(conn.execute("SELECT sql FROM sqlite_master WHERE name=?",(table,)).fetchone()[0].encode()).hexdigest())
                          for table in ('change_registry_items','change_registry_facts')],
                  schema_objects_sha256=hashlib.sha256(json.dumps(objects,ensure_ascii=False,separators=(',',':')).encode()).hexdigest())
        account=Account('synthetic-seller','ads');target=Target(11,101,contract_verified=True)
        conn.execute('BEGIN IMMEDIATE')
        with patch('packages.application.change_registry_search_cluster.needs_bidirectional_upgrade',return_value=False):
            prepare_in_transaction(conn,operation_id='intervening-addition',account=account,target=target,
                queries=['different exact'],created_at=AFTER,before_at=NOW,provenance={'fixture':True})
        conn.commit()
        old_ddl=conn.execute("SELECT sql FROM sqlite_master WHERE name='change_registry_items'").fetchone()[0]
        with self.assertRaisesRegex(RuntimeError,'reviewed registry rows changed'):
            ensure_change_registry_schema(conn,upgrade_bidirectional=True,reviewed_bidirectional_plan=plan)
        self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name='change_registry_items'").fetchone()[0],old_ddl)
        self.assertTrue(needs_bidirectional_upgrade(conn))
        conn.close()

    def test_runtime_startup_migrates_legacy_registry_before_runtime_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp)/'runtime');runtime.runtime_dir.mkdir()
            with sqlite3.connect(runtime.db_path) as conn:
                conn.executescript(legacy_sql());view_sql=add_orphan_finance_view(conn);conn.commit()
            # This is the actual registry startup path: WarehouseFunctionalBlock
            # builds CanonicalCostEngine, which calls the runtime bootstrap.
            WarehouseFunctionalBlock(runtime=runtime)
            with sqlite3.connect(runtime.db_path) as conn:
                self.assertEqual(len([r for r in conn.execute('PRAGMA table_info(change_registry_items)') if r[1]=='query_hash']),1)
                self.assertEqual(len([r for r in conn.execute('PRAGMA table_info(change_registry_facts)') if r[1]=='query_hash']),1)
                self.assertEqual(conn.execute('SELECT count(*) FROM change_registry_facts').fetchone()[0],3)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?",(ORPHAN_FINANCE_VIEW,)).fetchone()[0],view_sql)
                with self.assertRaisesRegex(sqlite3.OperationalError,'no such table'):
                    conn.execute('SELECT * FROM '+ORPHAN_FINANCE_VIEW).fetchall()
                before=(snapshot(conn),registry_objects(conn))
            WarehouseFunctionalBlock(runtime=runtime)
            with sqlite3.connect(runtime.db_path) as conn:
                self.assertEqual((snapshot(conn),registry_objects(conn)),before)

    def test_runtime_schema_refuses_legacy_migration_inside_owner_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn=sqlite3.connect(Path(tmp)/'legacy.sqlite3');conn.row_factory=sqlite3.Row;conn.executescript(legacy_sql())
            conn.execute('CREATE TABLE outer_marker(value TEXT)');conn.execute('BEGIN IMMEDIATE');conn.execute("INSERT INTO outer_marker VALUES('must_rollback')")
            with self.assertRaisesRegex(RuntimeError,'explicit setup outside a business transaction'):
                _ensure_schema(conn)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(conn.execute('SELECT count(*) FROM outer_marker').fetchone()[0],0)
            self.assertNotIn('query_hash',{r[1] for r in conn.execute('PRAGMA table_info(change_registry_items)')})
            conn.close()

    def test_populated_legacy_migration_reinitialize(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.executescript(legacy_sql());conn.execute('PRAGMA foreign_keys=ON')
        view_sql=add_orphan_finance_view(conn)
        settings=pragma_settings(conn)
        attempt_event_fks=[tuple(row) for row in conn.execute('PRAGMA foreign_key_list(change_registry_attempt_events)')]
        before=snapshot(conn);self.assertEqual(len(before['change_registry_facts'][1]),3)
        ensure_change_registry_schema(conn);conn.commit()
        for table,(columns,rows) in before.items():
            self.assertEqual([tuple(r) for r in conn.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY rowid')],rows)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[]);self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(pragma_settings(conn),settings)
        self.assertEqual([tuple(row) for row in conn.execute('PRAGMA foreign_key_list(change_registry_attempt_events)')],attempt_event_fks)
        self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?",(ORPHAN_FINANCE_VIEW,)).fetchone()[0],view_sql)
        with self.assertRaisesRegex(sqlite3.OperationalError,'no such table'):
            conn.execute('SELECT * FROM '+ORPHAN_FINANCE_VIEW).fetchall()
        exact=conn.serialize();ensure_change_registry_schema(conn);conn.commit();self.assertEqual(conn.serialize(),exact)
        for table in ('change_registry_items','change_registry_facts'):
            self.assertTrue(all(r[0] is None for r in conn.execute('SELECT query_hash FROM '+table)))
        conn.close()

    def test_failed_migration_rolls_back_complete_old_schema(self):
        conn=sqlite3.connect(':memory:',factory=FailingConnection);conn.row_factory=sqlite3.Row;conn.executescript(legacy_sql());conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA legacy_alter_table=ON')
        view_sql=add_orphan_finance_view(conn);settings=pragma_settings(conn)
        old=conn.serialize();conn.fail=True
        with self.assertRaises(sqlite3.OperationalError):ensure_change_registry_schema(conn)
        self.assertEqual(conn.serialize(),old);self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(pragma_settings(conn),settings)
        self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?",(ORPHAN_FINANCE_VIEW,)).fetchone()[0],view_sql)
        conn.fail=False;ensure_change_registry_schema(conn);conn.close()

    def test_exact_query_identity_rollback_and_repeated_evidence(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON');ensure_change_registry_schema(conn)
        account=Account('synthetic-seller','ads');target=Target(11,101,contract_verified=True);queries=['Буквальная строка','буквальная строка']
        kwargs=dict(operation_id='synthetic-operation',account=account,target=target,queries=queries,created_at=NOW,before_at=NOW,provenance={'fixture':True})
        conn.execute('BEGIN IMMEDIATE');prepare_in_transaction(conn,**kwargs);conn.rollback()
        for table in ('change_registry_operations','change_registry_items','change_registry_search_cluster_queries'):
            self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        conn.execute('BEGIN IMMEDIATE');ids=prepare_in_transaction(conn,**kwargs);conn.commit();self.assertEqual(len(set(ids.values())),2)
        evidence=dict(minus=queries,exact=True)
        conn.execute('BEGIN IMMEDIATE');facts=confirm_in_transaction(conn,operation_id='synthetic-operation',present_queries=queries,observed_at=AFTER,before_at=NOW,evidence=evidence);conn.commit()
        conn.execute('BEGIN IMMEDIATE');confirm_in_transaction(conn,operation_id='synthetic-operation',present_queries=queries,observed_at='2026-09-11T00:02:00Z',before_at=NOW,evidence=evidence);conn.commit()
        self.assertEqual(conn.execute('SELECT count(*) FROM change_registry_facts').fetchone()[0],2)
        self.assertEqual(conn.execute('SELECT count(*) FROM change_registry_search_cluster_readbacks').fetchone()[0],4)
        hashes=list(ids)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO change_registry_fact_links(fact_link_id,fact_id,link_kind,change_item_id,linked_at,evidence_digest) VALUES('cross-query',?,'change_item',?,'2026-09-11T00:02:00Z',?)",(facts[hashes[0]],ids[hashes[1]],canonical_digest('fixture')))
        conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):conn.execute("UPDATE change_registry_search_cluster_queries SET query='changed'")
        conn.rollback()
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[]);conn.close()

    def test_legacy_public_identity_and_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=ChangeRegistryRepository(Path(tmp));repo.initialize_schema()
            # Fresh synthetic old data loaded through existing schema constraints.
            with repo._transaction('fixture-seed') as conn:
                conn.execute("INSERT INTO change_registry_operations(operation_id,seller_id,account_scope,source_surface,actor_principal,actor_kind,requested_at,created_at,provenance_digest,mapping_version) VALUES('op','seller','account','fixture','owner','human','2026-09-11T00:00:00Z','2026-09-11T00:00:00Z',?,'wb_change_registry_mapping_v1')",(canonical_digest('legacy'),))
            identity=target_identity('price',nm_id=101)
            self.assertEqual(asdict(identity),dict(target_kind='price',nm_id=101,advert_id=0,placement=''))
            row=repo.append_change_item(change_item_id='item',operation_id='op',target=identity,parameter_field='original_price_minor',before_value=100,requested_value=200,created_at='2026-09-11T00:00:00Z')
            self.assertNotIn('query_hash',row);self.assertNotIn('query_hash',repo.read_operation('op')['items'][0])
            self.assertEqual(row,repo.append_change_item(change_item_id='item',operation_id='op',target=identity,parameter_field='original_price_minor',before_value=100,requested_value=200,created_at='2026-09-11T00:00:00Z'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path);args=p.parse_args()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RegistryTests))
    if args.output:args.output.write_text(json.dumps(dict(tests=result.testsRun,success=result.wasSuccessful()),indent=2))
    sys.exit(not result.wasSuccessful())
