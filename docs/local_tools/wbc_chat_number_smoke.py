"""Check concurrent allocation, retry safety and missing-counter refusal."""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4
from wbc_chat_number import allocate


def main():
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / 'numbers.sqlite3'
        try:
            allocate(db, str(uuid4()))
        except sqlite3.OperationalError:
            assert not db.exists()
        else:
            raise AssertionError('Missing counter was silently recreated')
        with sqlite3.connect(db) as conn:
            conn.executescript('''
                CREATE TABLE chats(thread_id TEXT PRIMARY KEY, number INTEGER NOT NULL);
                CREATE TABLE counter(id INTEGER PRIMARY KEY CHECK(id=1), last_number INTEGER NOT NULL);
                INSERT INTO counter VALUES(1, 162);
            ''')
        ids = [str(uuid4()) for _ in range(20)]
        with ProcessPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(allocate, [db] * 40, ids + ids))
        assert len(set(values)) == 20
        assert values[:20] == values[20:]
        assert sorted(set(values)) == list(range(163, 183))
        with sqlite3.connect(db) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        print('PASS: parallel callers, repeat calls, continuity and missing database')


if __name__ == '__main__':
    main()
