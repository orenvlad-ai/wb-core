#!/usr/bin/env python3
"""Allocate a WBC chat number in an already initialized local database."""
import argparse
from pathlib import Path
import sqlite3
from uuid import UUID

DEFAULT_DB = Path('/Users/Shared/WBC/chat-numbers.sqlite3')


def allocate(db: Path, thread_id: str) -> int:
    thread_id = str(UUID(thread_id))
    # Never recreate a lost counter: that could reissue existing numbers.
    with sqlite3.connect(db.resolve().as_uri() + '?mode=rw', uri=True, timeout=30) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT number FROM chats WHERE thread_id=?', (thread_id,)).fetchone()
        if row:
            return row[0]
        number = conn.execute('SELECT last_number FROM counter WHERE id=1').fetchone()[0] + 1
        conn.execute('INSERT INTO chats(thread_id, number) VALUES (?, ?)', (thread_id, number))
        conn.execute('UPDATE counter SET last_number=? WHERE id=1', (number,))
        return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('thread_id', help='ID of the main chat; reuse the original ID when continuing its task')
    args = parser.parse_args()
    try:
        print(f'{allocate(DEFAULT_DB, args.thread_id):04d}')
    except (sqlite3.Error, ValueError, TypeError) as exc:
        parser.exit(1, f'Номер не выдан: {exc}. Не используйте запасной счётчик.\n')


if __name__ == '__main__':
    main()
