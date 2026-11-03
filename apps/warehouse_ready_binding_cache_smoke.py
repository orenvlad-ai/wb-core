#!/usr/bin/env python3
"""Ready-only cache stays exact across mutable projections and SQLite rollback."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import warehouse_business_projection as projection
from packages.application import ready_publication as ready

DAY='2026-11-03'


def plan(version, extra=None):
    return json.dumps({'metadata':{'warehouse_history_coverage':{DAY:{'functional_version_id':version}, **(extra or {})}}})


class ReadyBindingCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'fixture.sqlite3'
        self.conn=sqlite3.connect(self.path);self.conn.row_factory=sqlite3.Row
        self.addCleanup(self.conn.close)
        c=self.conn
        c.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,plan_json,snapshot_id,plan_version,refreshed_at,activated_at)')
        c.execute('CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(version_id,status,business_effective_date,plan_fingerprint,published_at,created_at)')
        c.execute('CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(version_id,snapshot_date)')
        c.execute('CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(version_id,nm_id,warehouse_key)')
        for i in ('a','b','c'):
            c.execute('INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES(?,?,?,?,?,?)',(i,'good',DAY,i,DAY,DAY))
            c.execute('INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES(?,?)',(i,DAY))
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)',('base',DAY,plan('a'),'s','v',DAY,DAY))
        ready.ensure_publication_schema(c)
        projection.ensure_warehouse_business_projection_schema(c)
        c.commit();c.execute('BEGIN')

    def reconcile(self, conn=None, **kwargs):
        return projection.reconcile_warehouse_business_projection(conn or self.conn,target_dates=kwargs.pop('target_dates',[DAY]),**kwargs)

    def uncached(self, **kwargs):
        token=projection._READY_BINDING_CACHE.set(None)
        try:return self.reconcile(**kwargs)
        finally:projection._READY_BINDING_CACHE.reset(token)

    def test_hits_equal_uncached_and_mutable_projection_is_never_cached(self):
        expected=self.reconcile()
        with projection.ready_binding_cache(self.conn) as stats:
            self.assertEqual(expected,self.reconcile())
            self.assertEqual(expected,self.reconcile())
            self.conn.execute(f'INSERT INTO {projection.CURRENT_ROW_TABLE} VALUES(?,?,?,?,?,?,?,?)',(DAY,1,'r','{}','{}','{}','f',DAY))
            self.assertEqual(self.uncached(),self.reconcile())
            self.assertEqual(stats['ready_loads'],1)
            self.assertEqual(stats['cache_hits'],2)
            self.assertNotEqual(expected,self.reconcile())
        self.assertFalse(self.conn.execute("SELECT name FROM sqlite_temp_master WHERE type='trigger'").fetchall())

    def test_ready_update_insert_delete_disable_and_keep_exact(self):
        for sql,args in [('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('b'),)),
                ('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)',('new',DAY,plan('c'),'s','v',DAY,DAY)),
                ('DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=?',('new',))]:
            with projection.ready_binding_cache(self.conn) as stats:
                self.reconcile();self.conn.execute(sql,args)
                self.assertEqual(self.uncached(),self.reconcile())
                self.assertEqual(stats['cache_hits'],0)
                self.assertTrue(stats['disabled_reason'])

    def test_savepoint_rollback_revision_reuse_cannot_return_cached_binding(self):
        with projection.ready_binding_cache(self.conn) as stats:
            self.reconcile();self.conn.execute('SAVEPOINT caller')
            self.conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('b'),))
            self.assertEqual(self.uncached(),self.reconcile())
            self.conn.execute('ROLLBACK TO caller')
            self.conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('c'),))
            self.assertEqual(self.uncached(),self.reconcile())
            self.conn.execute('RELEASE caller')
            self.assertEqual(stats['ready_loads'],1)

    def test_full_rollback_then_begin_bypasses_old_context(self):
        with projection.ready_binding_cache(self.conn) as stats:
            self.reconcile();self.conn.rollback();self.conn.execute('BEGIN')
            self.conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('c'),))
            self.assertEqual(self.uncached(),self.reconcile())
            self.assertEqual(stats['cache_hits'],0)
            self.assertTrue(stats['disabled_reason'])

    def test_first_load_inside_savepoint_detects_reused_revision_after_rollback(self):
        with projection.ready_binding_cache(self.conn) as stats:
            self.conn.execute('SAVEPOINT caller')
            self.conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('b'),))
            first=self.reconcile()
            self.conn.execute('ROLLBACK TO caller')
            self.conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('c'),))
            after=self.reconcile()
            self.assertEqual(self.uncached(),after)
            self.assertNotEqual(first,after)
            self.assertEqual(stats['cache_hits'],0)
            self.assertTrue(stats['disabled_reason'])
            self.conn.execute('RELEASE caller')

    def test_other_connection_candidate_and_different_target_dates(self):
        with projection.ready_binding_cache(self.conn) as stats:
            self.reconcile()
            with closing(sqlite3.connect(self.path)) as other:
                other.row_factory=sqlite3.Row
                self.assertEqual(self.uncached(),self.reconcile(other))
            candidate=dict(bundle_version='base',as_of_date=DAY,plan_json=plan('b'))
            self.assertEqual(self.uncached(ready_candidate=candidate),self.reconcile(ready_candidate=candidate))
            self.assertEqual(self.uncached(target_dates=['2026-11-02']),self.reconcile(target_dates=['2026-11-02']))
            self.assertEqual(stats['ready_loads'],1)

    def test_missing_or_altered_revision_trigger_is_uncached(self):
        self.conn.execute('DROP TRIGGER ready_history_update')
        with projection.ready_binding_cache(self.conn) as stats:
            self.reconcile();self.conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(plan('b'),))
            self.assertEqual(self.uncached(),self.reconcile())
            self.assertEqual(stats['ready_loads'],0)
            self.assertTrue(stats['disabled_reason'])

    def test_context_requires_caller_transaction(self):
        self.conn.rollback()
        with self.assertRaisesRegex(ValueError,'owner_transaction_required'):
            with projection.ready_binding_cache(self.conn):pass


if __name__=='__main__':unittest.main()
