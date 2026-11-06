#!/usr/bin/env python3
"""Allocate a WBC chat number in an already initialized local database."""
import argparse
from pathlib import Path
import sqlite3
from uuid import UUID

DEFAULT_DB = Path('/Users/Shared/WBC/chat-numbers.sqlite3')
OPS_DB = Path('/Users/Shared/WBC/ops-chat-numbers.sqlite3')


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


def normalize_topic(topic: str) -> str:
    topic = ' '.join(topic.split()).lstrip('—–-:| ').strip()
    if not topic:
        raise ValueError('Укажите краткую тему чата')
    return topic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('thread_id', help='ID of the main chat; reuse the original ID when continuing its task')
    parser.add_argument('--title', help='Short topic; print the complete chat title without a separator')
    parser.add_argument('--series', choices=('dev', 'ops'), default='dev',
                        help='Independent local numbering series; ops uses suffix O')
    args = parser.parse_args()
    try:
        topic = normalize_topic(args.title) if args.title is not None else None
        number = allocate(OPS_DB if args.series == 'ops' else DEFAULT_DB, args.thread_id)
        label = f'{number:04d}' + ('O' if args.series == 'ops' else '')
        print(f'WBC {label} {topic}' if topic is not None else label)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        parser.exit(1, f'Номер не выдан: {exc}. Не используйте запасной счётчик.\n')


if __name__ == '__main__':
    main()
