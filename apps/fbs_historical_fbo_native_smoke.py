#!/usr/bin/env python3
"""Real native SQLite writes to disposable local fixtures only."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace, MethodType
from unittest.mock import patch
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.fbs_historical_fbo_dispatch_smoke import fixture
from packages.application.fbs_historical_fbo_dispatch import prepare_historical_fbo_dispatch_plan
from packages.application import fbs_historical_fbo_native as native
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect, _ensure_schema
from packages.application.ff_pool_foundation import FACILITIES_TABLE, FEATURE_EPOCHS_TABLE, BALANCES_TABLE, OPERATIONS_TABLE, LINES_TABLE
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.warehouse_functional import _ff_ledger_line_cost_snapshot
from packages.application.warehouse_business_projection import OUTBOX_TABLE

STAMP='2026-11-02T15:00:00Z'


class NativeHistoricalFboTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.runtime=SimpleNamespace(runtime_dir=root,db_path=root/'local.sqlite3')
        self.runtime.create_ff_stock_operation_guarded=MethodType(RegistryUploadDbBackedRuntime.create_ff_stock_operation_guarded,self.runtime)
        self.capture,self.scope=fixture()
        self.plan=prepare_historical_fbo_dispatch_plan(self.capture,self.scope)
        with closing(_connect(self.runtime.db_path)) as conn:
            _ensure_schema(conn)
            from packages.application.warehouse_business_projection import ensure_warehouse_projection_source_outbox
            ensure_warehouse_projection_source_outbox(conn)
            conn.execute(f'INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,?,?,?)',('synthetic-moscow','synthetic','Synthetic',1,'UTC',STAMP,STAMP))
            conn.execute(f'INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(?,?,?,?,?,?)',(1,1,1,'synthetic',STAMP,'{}'))
            for pool,q,cost in [('FBS',100,'10000'),('FBO',100,'6000')]:
                conn.execute(f'INSERT INTO {BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',('synthetic-moscow',pool,1,1,q,cost,str(int(cost)/q),'seed',STAMP))
            conn.execute(f'INSERT INTO {native.AGGREGATE}(operation_id,operation_type,source_type,source_key,created_at,total_quantity_delta,total_quantity_abs) VALUES(?,?,?,?,?,?,?)',('seed','receipt','synthetic','seed',STAMP,200,200))
            conn.execute(f'INSERT INTO {native.AGGREGATE_LINES}(operation_id,line_no,nm_id,quantity_delta) VALUES(?,?,?,?)',('seed',1,1,200))
            conn.commit();conn.execute('BEGIN')
            self.guard=native.capture_native_guard(conn,self.plan)
        self.book={'schema':native.SCHEMA,'active':True}
        self.load=patch.object(native,'load',return_value=(self.book,self.capture['book_version']))
        self.load.start();self.addCleanup(self.load.stop)

    def apply(self,**kw):
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            return native.apply_historical_fbo_native(self.runtime,plan=self.plan,capture=self.capture,scope=self.scope,
                expected_native_guard=self.guard,recapture=lambda conn:deepcopy(self.capture),owner_token=owner['owner_token'],
                posted_at=STAMP,actor='local-test',**kw)

    def snapshot(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            return {table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)] for table in
                [OPERATIONS_TABLE,LINES_TABLE,BALANCES_TABLE,native.AGGREGATE,native.AGGREGATE_LINES,OUTBOX_TABLE]}

    def test_atomic_frozen_native_and_same_identity_readback(self):
        result=self.apply();self.assertFalse(result['idempotent']);self.assertFalse(result['publication_complete'])
        before=self.snapshot()
        self.assertEqual(self.apply()['idempotent'],True)
        self.assertEqual(before,self.snapshot())
        with closing(_connect(self.runtime.db_path)) as conn:
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {OPERATIONS_TABLE}').fetchone()[0],4)
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {native.AGGREGATE}').fetchone()[0],4)
            row=dict(conn.execute(f"SELECT * FROM {native.AGGREGATE_LINES} WHERE operation_id=?",(native.operation_id(self.plan,0),)).fetchone())
            frozen=_ff_ledger_line_cost_snapshot(row)
            self.assertEqual(format(frozen['unit_cost_rub'],'f'),'50') # Current FBO WAC was 60.
            self.assertEqual(conn.execute(f"SELECT quantity FROM {BALANCES_TABLE} WHERE pool='FBS'").fetchone()[0],90)
            self.assertEqual(conn.execute(f"SELECT quantity FROM {BALANCES_TABLE} WHERE pool='FBO'").fetchone()[0],20)

    def test_long_existing_native_capital_tails_survive_exact_frozen_effects(self):
        tails={'FBS':'10000.'+'0'*55+'123456789','FBO':'6000.'+'0'*55+'987654321'}
        with closing(_connect(self.runtime.db_path)) as conn,conn:
            for pool,capital in tails.items():
                with localcontext() as context:
                    context.prec=160
                    wac=format(Decimal(capital)/100,'f')
                conn.execute(f"UPDATE {BALANCES_TABLE} SET capital_rub=?,wac_rub=? WHERE pool=?",(capital,wac,pool))
        self.refresh_guard();self.apply()
        with localcontext() as context:
            context.prec=160
            expected={pool:Decimal(value) for pool,value in tails.items()}
            for effect in self.plan['effects']:
                for line in effect['pool_movements']:
                    expected[line['pool']]+=Decimal(line['capital_delta_rub'])
            with closing(_connect(self.runtime.db_path)) as conn:
                for row in conn.execute(f'SELECT pool,quantity,capital_rub,wac_rub FROM {BALANCES_TABLE}'):
                    self.assertEqual(Decimal(row['capital_rub']),expected[row['pool']])
                    with localcontext() as ratio_context:
                        ratio_context.prec=50
                        self.assertEqual(Decimal(row['wac_rub']),expected[row['pool']]/row['quantity'])

    def test_each_failure_boundary_rolls_back_every_effect(self):
        before=self.snapshot()
        for phase in ('after_pool','after_aggregate'):
            for index in range(4):
                def crash(p,i):
                    if (p,i)==(phase,index):raise RuntimeError('injected')
                with self.assertRaisesRegex(RuntimeError,'injected'):self.apply(fault=crash)
                self.assertEqual(self.snapshot(),before)

    def test_current_cas_and_book_drift_write_nothing(self):
        before=self.snapshot()
        native.load.return_value=(self.book,'different-book')
        with self.assertRaisesRegex(ValueError,'book_compare_and_swap_failed'):self.apply()
        self.assertEqual(before,self.snapshot())
        native.load.return_value=(self.book,self.capture['book_version'])
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {BALANCES_TABLE} SET source_watermark='concurrent' WHERE pool='FBO'");conn.commit()
        changed=self.snapshot()
        with self.assertRaisesRegex(ValueError,'current_source_compare_and_swap_failed'):self.apply()
        self.assertEqual(changed,self.snapshot())

    def test_tampered_durable_money_is_conflict_not_retry(self):
        self.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {native.AGGREGATE_LINES} SET raw_json='{{}}' WHERE operation_id=?",(native.operation_id(self.plan,0),));conn.commit()
        changed=self.snapshot()
        with self.assertRaisesRegex(ValueError,'existing_aggregate_lines_conflict'):self.apply()
        self.assertEqual(changed,self.snapshot())

    def test_inactive_failed_missing_or_wrong_schema_book_cannot_write(self):
        for book in (None,{}, {'schema':'wrong','active':True},
                     {'schema':native.SCHEMA,'active':False},
                     {'schema':native.SCHEMA,'active':True,'publication_error':{'reason':'incomplete_period_cannot_close'}}):
            with self.subTest(book=book):
                native.load.return_value=(book,self.capture['book_version'])
                self.assert_blocked_without_changes('healthy_active_book_required')

    def refresh_guard(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN')
            self.guard=native.capture_native_guard(conn,self.plan)

    def assert_blocked_without_changes(self,reason):
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError,reason):self.apply()
        self.assertEqual(before,self.snapshot())

    def seed_foreign_fbo_reservation(self,quantity,*,origin=True):
        from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('INSERT INTO sheet_vitrina_v1_ff_stock_reservation_operations VALUES(?,?,?,?,?,?,?)',
                ('foreign-reserve','foreign-reserve','999','foreign-revision','reserve',STAMP,'{}'))
            conn.execute('INSERT INTO sheet_vitrina_v1_ff_stock_reservation_lines VALUES(?,?,?,?,?)',
                ('foreign-reserve',1,1,quantity,'{}'))
            if origin:
                conn.execute(f'INSERT INTO {ASSIGNMENTS_TABLE}(assignment_id,request_id,request_fingerprint,wb_supply_cache_key,wb_supply_id,source_revision,feature_epoch,facility_id,pool,actor,assigned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    ('foreign-origin','foreign-origin','foreign-fingerprint','supply:999','999','foreign-revision',1,'synthetic-moscow','FBO','test',STAMP))
            conn.commit()

    def seed_fbs_reservation(self,quantity):
        from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE
        from packages.application.ff_pool_cutover import MANIFESTS_TABLE
        with closing(_connect(self.runtime.db_path)) as conn:
            columns=[row[1] for row in conn.execute(f'PRAGMA table_info({MANIFESTS_TABLE})')]
            values={name:'synthetic' for name in columns}
            values.update(cutover_id='synthetic-cutover',deployed_sha='a'*40,cutover_at=STAMP,business_date='2026-10-15',feature_epoch=1,
                          observation_watermark_sequence=0,created_at=STAMP,manifest_json='{}')
            conn.execute(f"INSERT INTO {MANIFESTS_TABLE}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",[values[c] for c in columns])
            conn.execute(f'INSERT INTO {CURRENT_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('synthetic-cutover',9001,'reserved',1,'synthetic-source','synthetic-status','new','waiting','synthetic-moscow','FBS',1,quantity,'100','',STAMP))
            conn.commit()

    def test_foreign_fbo_reserve_and_unknown_origin_reject_before_append(self):
        self.seed_foreign_fbo_reservation(21);self.refresh_guard()
        self.assert_blocked_without_changes('зарезервированный')

    def test_unknown_foreign_fbo_origin_rejects_before_append(self):
        self.seed_foreign_fbo_reservation(1,origin=False);self.refresh_guard()
        self.assert_blocked_without_changes('резерва')

    def test_fbs_lifecycle_reserved_stock_cannot_fund_reallocation(self):
        self.seed_fbs_reservation(91);self.refresh_guard()
        self.assert_blocked_without_changes('зарезервированный')

    def test_new_reservation_evidence_changes_guard_before_append(self):
        self.seed_fbs_reservation(1)
        self.assert_blocked_without_changes('current_source_compare_and_swap_failed')

    def test_unresolved_fbs_order_dependency_rejects_before_append(self):
        from packages.application.wb_fbs_orders import OBSERVATIONS_TABLE,WAREHOUSE_MAPPINGS_TABLE
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f'INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES(?,?,?,?,?,?,?)',('synthetic-mapping',9,'synthetic-moscow','synthetic-mapping',1,STAMP,'test'))
            conn.execute(f'INSERT INTO {OBSERVATIONS_TABLE}(observation_id,order_id,source_revision,delivery_type,warehouse_id,nm_id,observed_at,collector_date_from,collector_date_to,collector_cursor) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('synthetic-order',9001,'synthetic-revision','fbs',9,1,STAMP,1,2,0));conn.commit()
        self.refresh_guard();self.assert_blocked_without_changes('точным резервом')

    def test_fbs_open_reconciliation_rejects_before_append(self):
        from packages.application.ff_pool_fbs_lifecycle import EVENTS_TABLE,RECONCILIATION_TABLE
        with closing(_connect(self.runtime.db_path)) as conn:
            columns=[row[1] for row in conn.execute(f'PRAGMA table_info({EVENTS_TABLE})') if row[1]!='event_sequence']
            values={name:'synthetic' for name in columns}
            values.update(event_id='synthetic-reconciliation-event',cutover_id='synthetic-cutover',order_id=9002,
                episode_sequence=1,event_type='post_handoff_reconciliation',source_order_observation_sequence=0,
                source_status_observation_sequence=0,source_observed_at=STAMP,facility_id='synthetic-moscow',
                pool='FBS',nm_id=1,quantity=1,physical_quantity_delta=0,capital_delta_rub='0',frozen_wac_rub='100',
                occurred_at=STAMP,details_json='{}')
            conn.execute(f"INSERT INTO {EVENTS_TABLE}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",[values[c] for c in columns])
            conn.execute(f'INSERT INTO {RECONCILIATION_TABLE} VALUES(?,?,?,?,?,?,?,?)',
                ('synthetic-lane','synthetic-cutover',9002,'synthetic-reconciliation-event','synthetic','synthetic','open',STAMP))
            conn.commit()
        self.refresh_guard();self.assert_blocked_without_changes('открыто расхождение')

    def test_exact_reserved_remainder_is_preserved(self):
        self.seed_fbs_reservation(90);self.seed_foreign_fbo_reservation(10);self.refresh_guard()
        self.assertEqual(self.apply()['status'],'native_committed_pending_publication')

    def test_positive_quantity_zero_capital_is_rejected_and_rolled_back(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {BALANCES_TABLE} SET capital_rub='1000',wac_rub='10' WHERE pool='FBS'");conn.commit()
        self.refresh_guard();self.assert_blocked_without_changes('current_projection_would_be_invalid')

    def test_inactive_facility_is_rejected_and_fenced(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {FACILITIES_TABLE} SET active=0 WHERE facility_id='synthetic-moscow'");conn.commit()
        self.assert_blocked_without_changes('current_source_compare_and_swap_failed')
        self.refresh_guard();self.assert_blocked_without_changes('facility_inactive_or_missing')

    def guarded_arguments(self,identity):
        return dict(operation_id=identity,operation_type='writeoff',source_type='synthetic',source_key=identity,
            source_object_id=identity,source_object_label=identity,created_at=STAMP,created_by='test',warnings=[],diagnostics={},
            lines=[dict(nm_id=1,quantity_delta=-1)],expected_balances={1:200})

    def test_connection_owned_guard_never_commits_rolls_back_or_closes(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE');trace=[];conn.set_trace_callback(trace.append)
            result=self.runtime.create_ff_stock_operation_guarded(connection=conn,**self.guarded_arguments('caller-owned'))
            self.assertFalse(result['idempotent']);self.assertTrue(conn.in_transaction)
            self.assertFalse(any(sql.upper().startswith(('COMMIT','ROLLBACK')) for sql in trace))
            self.assertEqual(conn.execute('SELECT 1').fetchone()[0],1)
            conn.rollback()
            self.assertIsNone(conn.execute(f'SELECT 1 FROM {native.AGGREGATE} WHERE operation_id=?',('caller-owned',)).fetchone())
            conn.execute('BEGIN IMMEDIATE');conn.execute('CREATE TABLE caller_marker(value)');conn.execute('INSERT INTO caller_marker VALUES(1)')
            arguments=self.guarded_arguments('caller-fails');arguments['expected_balances']={1:0}
            with self.assertRaisesRegex(ValueError,'balances_changed'):
                self.runtime.create_ff_stock_operation_guarded(connection=conn,**arguments)
            self.assertTrue(conn.in_transaction);self.assertEqual(conn.execute('SELECT value FROM caller_marker').fetchone()[0],1)
            conn.rollback()

    def test_default_guard_preserves_own_commit_and_rollback(self):
        self.runtime.create_ff_stock_operation_guarded(**self.guarded_arguments('own-commit'))
        with closing(_connect(self.runtime.db_path)) as conn:
            self.assertIsNotNone(conn.execute(f'SELECT 1 FROM {native.AGGREGATE} WHERE operation_id=?',('own-commit',)).fetchone())
            conn.execute(f"CREATE TRIGGER local_test_fail BEFORE INSERT ON {native.AGGREGATE_LINES} WHEN NEW.operation_id='own-fails' BEGIN SELECT RAISE(ABORT,'injected-own'); END");conn.commit()
        arguments=self.guarded_arguments('own-fails');arguments['expected_balances']={1:199}
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected-own'):
            self.runtime.create_ff_stock_operation_guarded(**arguments)
        with closing(_connect(self.runtime.db_path)) as conn:
            self.assertIsNone(conn.execute(f'SELECT 1 FROM {native.AGGREGATE} WHERE operation_id=?',('own-fails',)).fetchone())

    def test_no_owner_and_wrong_connection_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'admission owner'):
            native.apply_historical_fbo_native(self.runtime,plan=self.plan,capture=self.capture,scope=self.scope,
                expected_native_guard=self.guard,recapture=lambda c:self.capture,owner_token='invalid',posted_at=STAMP,actor='test')
        with closing(_connect(self.runtime.db_path)) as conn:
            with self.assertRaisesRegex(ValueError,'owned_connection_invalid'):
                self.runtime.create_ff_stock_operation_guarded(connection=conn,operation_id='bad',operation_type='writeoff',source_type='test',source_key='bad',source_object_id='bad',source_object_label='bad',created_at=STAMP,created_by='test',warnings=[],diagnostics={},lines=[dict(nm_id=1,quantity_delta=-1)],expected_balances={1:200})


if __name__=='__main__':unittest.main()
