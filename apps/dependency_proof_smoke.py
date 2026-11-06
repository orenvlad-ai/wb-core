"""Native SQLite revision/absence and final-check-to-commit race fixtures."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from packages.application.dependency_proof import DependencyProof, CompletionProofError, install_dependency_revisions, readonly_deadline


class DependencyProofTests(unittest.TestCase):
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

    def test_python_budget_interrupts_readonly_work(self):
        path=self.fixture('DELETE'); conn,_=self.capture(path)
        with self.assertRaisesRegex(CompletionProofError,'budget_exhausted'):
            with readonly_deadline(conn,time.monotonic()+.01):
                value=0
                for i in range(1000000): value+=i
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute('SELECT value FROM source').fetchone()[0],'a')


if __name__=='__main__': unittest.main()
