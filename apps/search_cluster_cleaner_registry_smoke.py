#!/usr/bin/env python3
"""Populated legacy migration, rollback, exact query items/facts and evidence."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.change_registry import ensure_change_registry_schema,ChangeRegistryRepository,target_identity,canonical_digest
from packages.application.change_registry_search_cluster import prepare_in_transaction,confirm_in_transaction
from packages.contracts.search_cluster_cleaner import Account,Target

LEGACY=Path(__file__).parent/'fixtures/search_cluster_cleaner_legacy_registry.json'
def legacy_sql():
    fixture=json.loads(LEGACY.read_text(encoding='utf-8'))
    assert fixture['schema']=='wb-core.synthetic-legacy-registry/v1'
    sql=fixture['sql']
    assert isinstance(sql,str) and hashlib.sha256(sql.encode('utf-8')).hexdigest()==fixture['sql_sha256']
    return sql


NOW='2026-09-11T00:00:00+00:00';AFTER='2026-09-11T00:01:00+00:00'


def snapshot(conn):
    result={}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'change_registry_%' ORDER BY name").fetchall():
        columns=[r[1] for r in conn.execute('PRAGMA table_info('+table+')')]
        result[table]=(columns,[tuple(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')])
    return result


class FailingConnection(sqlite3.Connection):
    fail=False
    def execute(self,sql,*args,**kwargs):
        if self.fail and sql.startswith('INSERT INTO change_registry_facts_new'):raise sqlite3.OperationalError('synthetic migration copy failure')
        return super().execute(sql,*args,**kwargs)


class RegistryTests(unittest.TestCase):
    def test_populated_legacy_migration_reinitialize(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.executescript(legacy_sql());conn.execute('PRAGMA foreign_keys=ON')
        before=snapshot(conn);self.assertEqual(len(before['change_registry_facts'][1]),3)
        ensure_change_registry_schema(conn);conn.commit()
        for table,(columns,rows) in before.items():
            self.assertEqual([tuple(r) for r in conn.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY rowid')],rows)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[]);self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        exact=conn.serialize();ensure_change_registry_schema(conn);conn.commit();self.assertEqual(conn.serialize(),exact)
        for table in ('change_registry_items','change_registry_facts'):
            self.assertTrue(all(r[0] is None for r in conn.execute('SELECT query_hash FROM '+table)))
        conn.close()

    def test_failed_migration_rolls_back_complete_old_schema(self):
        conn=sqlite3.connect(':memory:',factory=FailingConnection);conn.row_factory=sqlite3.Row;conn.executescript(legacy_sql());conn.execute('PRAGMA foreign_keys=ON')
        old=conn.serialize();conn.fail=True
        with self.assertRaises(sqlite3.OperationalError):ensure_change_registry_schema(conn)
        self.assertEqual(conn.serialize(),old);self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0],1)
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
