"""Native SQLite revision/absence and final-check-to-commit race fixtures."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.dependency_proof import DependencyProof, CompletionProofError, install_dependency_revisions, readonly_deadline, check_deadline, DetachedReadConnection


class DependencyProofTests(unittest.TestCase):
    def test_nested_attached_views_track_base_tables_and_schema(self):
        for mode in ('DELETE', 'WAL'):
            with self.subTest(mode=mode):
                main, raw = self.fixture(mode), self.fixture(mode)
                with sqlite3.connect(raw) as writer:
                    writer.execute('CREATE VIEW current_rows AS SELECT id,value FROM source')
                    writer.execute('CREATE VIEW nested_rows AS SELECT * FROM current_rows')
                reader = sqlite3.connect(main.as_uri()+'?mode=ro', uri=True)
                self.addCleanup(reader.close)
                reader.execute('ATTACH DATABASE ? AS finance_raw_store', (raw.as_uri()+'?mode=ro',))
                reader.execute('CREATE TEMP VIEW raw_rows AS SELECT * FROM finance_raw_store.nested_rows')
                reader.execute('PRAGMA query_only=ON')
                proof = DependencyProof.capture(reader)
                with proof.observe():
                    self.assertEqual(reader.execute('SELECT value FROM raw_rows').fetchall(), [('a',)])
                receipt = proof.seal()
                self.assertEqual({(x['schema'], x['table']) for x in receipt['dependencies']},
                                 {('finance_raw_store', 'source')})
                with sqlite3.connect(raw) as writer:
                    writer.execute('INSERT INTO unrelated VALUES("irrelevant")')
                proof.assert_current(reader)
                with sqlite3.connect(raw) as writer:
                    writer.execute('UPDATE source SET value="b"')
                with self.assertRaisesRegex(CompletionProofError, 'dependency_revision_changed'):
                    proof.assert_current(reader)
                proof = DependencyProof.capture(reader)
                with proof.observe(): reader.execute('SELECT value FROM raw_rows').fetchall()
                proof.seal()
                with sqlite3.connect(raw) as writer:
                    writer.execute('DROP VIEW current_rows')
                    writer.execute('CREATE VIEW current_rows AS SELECT id,"changed" AS value FROM source')
                with self.assertRaisesRegex(CompletionProofError, 'dependency_schema_changed'):
                    proof.assert_current(reader)

    def test_view_permission_does_not_allow_revision_table_or_unknown_store(self):
        main, raw = self.fixture('DELETE'), self.fixture('DELETE')
        with sqlite3.connect(main) as writer:
            writer.execute('CREATE VIEW protected_rows AS SELECT * FROM wbc_dependency_revisions')
        reader = sqlite3.connect(main.as_uri()+'?mode=ro', uri=True)
        self.addCleanup(reader.close)
        reader.execute('PRAGMA query_only=ON')
        proof = DependencyProof.capture(reader)
        with proof.observe(), self.assertRaises(sqlite3.DatabaseError):
            reader.execute('SELECT * FROM protected_rows').fetchall()
        with sqlite3.connect(raw) as writer:
            writer.execute('CREATE VIEW current_rows AS SELECT * FROM source')
        reader.execute('ATTACH DATABASE ? AS unknown_store', (raw.as_uri()+'?mode=ro',))
        with proof.observe(), self.assertRaises(sqlite3.DatabaseError):
            reader.execute('SELECT * FROM unknown_store.current_rows').fetchall()

    def test_view_added_after_capture_is_not_authorized(self):
        path = self.fixture('DELETE')
        reader = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        self.addCleanup(reader.close)
        reader.execute('PRAGMA query_only=ON')
        proof = DependencyProof.capture(reader)
        with sqlite3.connect(path) as writer:
            writer.execute('CREATE VIEW late_view AS SELECT * FROM source')
        with proof.observe(), self.assertRaises(sqlite3.DatabaseError):
            reader.execute('SELECT * FROM late_view').fetchall()

    def test_interrupted_first_install_rolls_back_trigger_and_counter_together(self):
        class Interrupted(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                result = super().execute(sql, *args, **kwargs)
                if sql.startswith('CREATE TRIGGER'):
                    raise RuntimeError('crash after first trigger')
                return result
        for mode in ('DELETE', 'WAL'):
            root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            path = root / 'install.sqlite3'
            conn = sqlite3.connect(path, factory=Interrupted)
            conn.execute('PRAGMA journal_mode='+mode)
            conn.execute('CREATE TABLE source(id INTEGER PRIMARY KEY, value TEXT)')
            try:
                with self.assertRaisesRegex(RuntimeError, 'crash'), conn:
                    install_dependency_revisions(conn)
            finally:
                conn.close()
            with sqlite3.connect(path) as recovered:
                self.assertEqual(recovered.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 0)
                self.assertEqual(recovered.execute("SELECT count(*) FROM sqlite_master WHERE name='wbc_dependency_revisions'").fetchone()[0], 0)
                install_dependency_revisions(recovered)
            _, proof = self.capture(path)

    def fixture(self, mode):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / 'source.sqlite3'
        conn = sqlite3.connect(path)
        conn.execute('PRAGMA journal_mode='+mode)
        conn.executescript('CREATE TABLE source(id INTEGER PRIMARY KEY, value TEXT); CREATE TABLE unrelated(value TEXT); INSERT INTO source VALUES(1,"a");')
        install_dependency_revisions(conn); conn.commit(); conn.close()
        return path

    def capture(self, path):
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute('PRAGMA query_only=ON')
        proof = DependencyProof.capture(conn)
        with proof.observe():
            conn.execute('SELECT value FROM source WHERE id IN (1,2)').fetchall()
        proof.seal()
        return conn, proof

    def test_unrelated_commits_do_not_conflict_but_aba_and_phantoms_do(self):
        for mode in ('WAL','DELETE'):
            with self.subTest(mode=mode):
                path = self.fixture(mode); conn, proof = self.capture(path)
                with sqlite3.connect(path) as writer:
                    writer.execute('INSERT INTO unrelated VALUES("status")')
                proof.assert_current(conn)
                with sqlite3.connect(path) as writer:
                    writer.execute('UPDATE source SET value="b"'); writer.execute('UPDATE source SET value="a"')
                with self.assertRaises(CompletionProofError): proof.assert_current(conn)
                conn, proof = self.capture(path)
                with sqlite3.connect(path) as writer: writer.execute('INSERT INTO source VALUES(2,"phantom")')
                with self.assertRaises(CompletionProofError): proof.assert_current(conn)

    def test_missing_trigger_new_table_and_uninstrumented_store_fail_closed(self):
        path = self.fixture('DELETE'); conn, proof = self.capture(path)
        with sqlite3.connect(path) as writer: writer.execute('CREATE TABLE fallback(value TEXT)')
        with self.assertRaises(CompletionProofError): proof.assert_current(conn)
        with self.assertRaises(CompletionProofError): DependencyProof.capture(conn)
        with sqlite3.connect(path) as writer:
            install_dependency_revisions(writer)
            name=writer.execute('SELECT name FROM sqlite_master WHERE type="trigger" LIMIT 1').fetchone()[0]
            writer.execute('DROP TRIGGER "'+name+'"')
        with self.assertRaises(CompletionProofError): DependencyProof.capture(conn)
        with sqlite3.connect(path) as writer, self.assertRaises(CompletionProofError): install_dependency_revisions(writer)

    def test_reserved_split_stores_block_last_check_to_commit_writer(self):
        for mode in ('DELETE','WAL'):
            main = self.fixture(mode); raw = self.fixture(mode)
            reader = sqlite3.connect(main); self.addCleanup(reader.close)
            reader.execute('ATTACH DATABASE ? AS raw', (str(raw),)); reader.execute('PRAGMA query_only=ON')
            proof=DependencyProof.capture(reader)
            with proof.observe():
                reader.execute('SELECT value FROM main.source').fetchall()
                reader.execute('SELECT value FROM raw.source').fetchall()
            proof.seal()
            with sqlite3.connect(main) as writer:
                writer.execute('ATTACH DATABASE ? AS raw', (str(raw),))
                writer.execute('BEGIN IMMEDIATE'); proof.assert_current(writer)
                outcomes=[]
                def mutate():
                    for file in (main,raw):
                        try:
                            with sqlite3.connect(file,timeout=.02) as concurrent:
                                concurrent.execute('INSERT INTO source VALUES(2,"race")')
                        except sqlite3.OperationalError: outcomes.append('blocked')
                thread=threading.Thread(target=mutate); thread.start(); thread.join(2)
                self.assertFalse(thread.is_alive()); self.assertEqual(outcomes,['blocked','blocked'])
                writer.execute('INSERT INTO unrelated VALUES("own")'); writer.commit()
            proof.assert_current(reader)
            with sqlite3.connect(raw) as concurrent: concurrent.execute('DELETE FROM source')
            with self.assertRaises(CompletionProofError): proof.assert_current(reader)

    def test_delete_detaches_reader_before_python_processing(self):
        path=self.fixture('DELETE');conn,_=self.capture(path)
        cursor=DetachedReadConnection(conn).execute('SELECT value FROM source')
        # Cursor has not been consumed by its Python caller; DELETE COMMIT must
        # still succeed. A live sqlite cursor would hold its SHARED read lock.
        with sqlite3.connect(path,timeout=.01) as writer:
            writer.execute('INSERT INTO unrelated VALUES("while-processing")')
        self.assertEqual([r[0] for r in cursor],['a'])

    def test_diagnostics_are_bounded_and_report_changed_domain(self):
        value=CompletionProofError('SELECT secret',phase='token:secret',classification='provider-secret',business_write_started='secret').diagnostic()
        self.assertNotIn('secret',json.dumps(value));self.assertFalse(value['business_write_started'])
        path=self.fixture('DELETE');conn,proof=self.capture(path)
        with sqlite3.connect(path) as writer:writer.execute('DELETE FROM source')
        with self.assertRaises(CompletionProofError) as caught:proof.assert_current(conn)
        diagnostic=caught.exception.diagnostic()
        self.assertEqual(diagnostic['changed_schemas'],['main']);self.assertEqual(diagnostic['changed_dependency_count'],1)
        self.assertNotEqual(diagnostic['before_vector_digest'],diagnostic['after_vector_digest'])

    def test_finance_diagnostic_is_typed_allowlisted_and_bounded(self):
        digest='sha256:'+'a'*64
        error=CompletionProofError('provider secret',phase='provider secret',classification='provider secret')
        error.before_vector_digest='provider secret'
        error.finance_handoff=dict(
            failed_condition=['provider secret'],
            captured=dict(date_from='2026-09-08',date_to='provider secret',fingerprint=digest,
                target_before_digest=True,cost_snapshot_version=digest,
                non_target_preserved=digest,provider_message='provider secret'),
            applied=dict(status='provider secret',accounting_version='provider secret',
                post_verify_stale_week_count=True,source_advanced_after_apply='provider secret'),
            observed_after=dict(cost_snapshot_effective_date='2026-99-99',cost_snapshot_version=digest),
            applied_source_dependency_digest='provider secret',post_source_dependency_digest=digest,
            provider_message='provider secret'*10000)
        diagnostic=error.diagnostic()
        self.assertEqual(diagnostic['reason'],'dependency_proof_failed')
        self.assertNotIn('provider secret',json.dumps(diagnostic))
        self.assertLess(len(json.dumps(diagnostic)),2500)
        handoff=diagnostic['finance_handoff']
        self.assertEqual(handoff['failed_condition'],'unknown')
        self.assertEqual(handoff['captured']['date_from'],'2026-09-08')
        self.assertEqual(handoff['captured']['fingerprint'],digest)
        self.assertIsNone(handoff['captured']['date_to'])
        self.assertIsNone(handoff['captured']['target_before_digest'])
        self.assertIsNone(handoff['captured']['non_target_preserved'])
        self.assertIsNone(handoff['applied']['post_verify_stale_week_count'])
        self.assertIsNone(handoff['applied_source_dependency_digest'])
        self.assertEqual(handoff['post_source_dependency_digest'],digest)
        self.assertEqual(handoff['observed_after']['cost_snapshot_version'],digest)
        self.assertIsNone(handoff['observed_after']['cost_snapshot_effective_date'])
        for reason in ('fingerprint','status','non_target','stale_weeks','source_advanced'):
            error.finance_handoff['failed_condition']=reason
            self.assertEqual(error.diagnostic()['finance_handoff']['failed_condition'],reason)

    def test_same_inode_sqlite_restore_invalidates_schema_cookie(self):
        path=self.fixture('DELETE');conn,proof=self.capture(path)
        backup=sqlite3.connect(str(path)+'.backup');self.addCleanup(backup.close)
        conn.backup(backup)
        with sqlite3.connect(path) as writer:backup.backup(writer)
        with self.assertRaisesRegex(CompletionProofError,'schema_changed'):proof.assert_current(conn)

    def test_checked_in_manifest_matches_trigger_contract(self):
        from packages.application.dependency_proof import _trigger
        import hashlib
        manifest=json.loads((Path(__file__).resolve().parents[1]/'docs/contracts/finance_completion_dependency_manifest.json').read_text())
        self.assertEqual(manifest['proof_contract'],'sqlite_dependency_proof_v1')
        for item in manifest['fixture_observed_dependencies']:
            for trigger in item['triggers']:
                name,sql=_trigger(item['table'],trigger['event'])
                self.assertEqual(trigger['name'],name)
                self.assertEqual(trigger['sql_sha256'],hashlib.sha256(sql.encode()).hexdigest())

    def test_python_budget_interrupts_readonly_work(self):
        path=self.fixture('DELETE'); conn,_=self.capture(path)
        with self.assertRaisesRegex(CompletionProofError,'budget_exhausted'):
            with readonly_deadline(conn,time.monotonic()+.01):
                value=0
                for i in range(1000000):
                    check_deadline()
                    value+=i
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute('SELECT value FROM source').fetchone()[0],'a')


if __name__=='__main__': unittest.main()
