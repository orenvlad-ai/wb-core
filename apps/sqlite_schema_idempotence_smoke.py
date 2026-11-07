"""Native schema initialization must preserve detached completion proofs."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.dependency_proof import DependencyProof, CompletionProofError, install_dependency_revisions
from packages.application.finance_raw_storage import ensure_raw_schema, RAW_CURRENT_ROWS_SQL
from packages.application.ff_pool_documents import ensure_ff_pool_document_schema, OVERHEAD_RENEWAL_SOURCE_GUARD_SQL
from packages.application.sqlite_schema import ensure_schema_definition


class SchemaIdempotenceTests(unittest.TestCase):
    def store(self, mode, ensure):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / 'store.sqlite3'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode='+mode)
            ensure(conn)
            install_dependency_revisions(conn)
        reader = self.enterContext(closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)))
        reader.execute('PRAGMA query_only=ON')
        return path, reader

    def test_repeated_initializers_preserve_schema_and_every_revision(self):
        for mode in ('DELETE', 'WAL'):
            for ensure in (ensure_raw_schema, ensure_ff_pool_document_schema):
                with self.subTest(mode=mode, ensure=ensure.__name__):
                    path, reader = self.store(mode, ensure)
                    proof = DependencyProof.capture(reader)
                    with proof.observe():
                        if ensure is ensure_raw_schema:
                            reader.execute('SELECT * FROM finance_raw_schema_meta').fetchall()
                            reader.execute('SELECT * FROM finance_raw_current_rows').fetchall()
                    schema = list(reader.execute('SELECT type,name,sql FROM sqlite_master'))
                    revisions = list(reader.execute('SELECT * FROM wbc_dependency_revisions ORDER BY table_name'))
                    for _ in range(3):
                        with closing(sqlite3.connect(path)) as writer, writer:
                            ensure(writer)
                        proof.seal()
                    self.assertEqual(schema, list(reader.execute('SELECT type,name,sql FROM sqlite_master')))
                    self.assertEqual(revisions, list(reader.execute('SELECT * FROM wbc_dependency_revisions ORDER BY table_name')))

    def test_runtime_cache_miss_after_revision_install_preserves_proof(self):
        from packages.application.registry_upload_db_backed_runtime import _ensure_schema, _SCHEMA_READY_KEYS, _schema_ready_key
        def mature_runtime(conn):
            _ensure_schema(conn)
            # The first fresh bootstrap adds projection tables after installing
            # domain guards. Complete those one-time guards before the proof.
            _SCHEMA_READY_KEYS.discard(_schema_ready_key(conn))
            _ensure_schema(conn)
        for mode in ('DELETE', 'WAL'):
            path, reader = self.store(mode, mature_runtime)
            proof = DependencyProof.capture(reader)
            with proof.observe():
                reader.execute('SELECT * FROM sheet_vitrina_v1_ff_pool_documents').fetchall()
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.row_factory = sqlite3.Row
                self.assertNotIn(_schema_ready_key(writer), _SCHEMA_READY_KEYS)
                _ensure_schema(writer)
                self.assertIn(_schema_ready_key(writer), _SCHEMA_READY_KEYS)
            proof.seal()

    def test_old_definitions_migrate_once_and_invalidate_old_proof(self):
        cases = (
            (ensure_raw_schema, 'view', 'finance_raw_current_rows', RAW_CURRENT_ROWS_SQL,
             'CREATE VIEW finance_raw_current_rows AS SELECT 1 AS obsolete'),
            (ensure_ff_pool_document_schema, 'trigger', 'ff_pool_overhead_payment_renewal_source_guard',
             OVERHEAD_RENEWAL_SOURCE_GUARD_SQL,
             'CREATE TRIGGER ff_pool_overhead_payment_renewal_source_guard BEFORE INSERT ON '
             'sheet_vitrina_v1_ff_pool_overhead_payment_renewals BEGIN SELECT 1; END'),
        )
        for ensure, kind, name, expected, old in cases:
            path, reader = self.store('DELETE', ensure)
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute(f'DROP {kind} {name}')
                writer.execute(old)
            proof = DependencyProof.capture(reader)
            with closing(sqlite3.connect(path)) as writer, writer:
                ensure(writer)
            with self.assertRaisesRegex(CompletionProofError, 'dependency_schema_changed') as caught:
                proof.seal()
            diagnostic = caught.exception.diagnostic()
            self.assertEqual(diagnostic['changed_schemas'], ['main'])
            self.assertEqual(diagnostic['after_schema_version']-diagnostic['before_schema_version'], 2)
            self.assertEqual(reader.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0], expected)
            proof = DependencyProof.capture(reader)
            with closing(sqlite3.connect(path)) as writer, writer:
                ensure(writer)
            proof.seal()

    def test_failed_replacement_restores_old_object_and_caller_transaction(self):
        path, reader = self.store('DELETE', ensure_raw_schema)
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('CREATE TABLE pending(value TEXT)')
            writer.execute("INSERT INTO pending VALUES('caller-owned')")
            with self.assertRaises(sqlite3.OperationalError):
                ensure_schema_definition(writer, kind='view', name='finance_raw_current_rows',
                    sql='CREATE VIEW finance_raw_current_rows AS invalid SQL')
            self.assertTrue(writer.in_transaction)
            self.assertEqual(writer.execute('SELECT sql FROM sqlite_master WHERE name=?',
                ('finance_raw_current_rows',)).fetchone()[0], RAW_CURRENT_ROWS_SQL)
            writer.rollback()
        self.assertEqual(reader.execute('SELECT count(*) FROM pending').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
