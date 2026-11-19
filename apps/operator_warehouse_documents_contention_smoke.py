"""DELETE-journal contention preserves operator status and completed receipts."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.operator_warehouse_documents_smoke import OperatorDocuments, operations
from apps.operator_fulfillment_services_contention_smoke import blocked_commit
from packages.application.sqlite_contention import SQLiteContentionExhausted


def row(runtime, identity):
    with closing(operations.readonly(runtime.db_path)) as conn:
        return dict(conn.execute('SELECT * FROM ' + operations.TABLE + ' WHERE request_id=?', (identity,)).fetchone())


def main():
    case = OperatorDocuments()
    case.setUp()
    try:
        runtime = case.runtime
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        with patch.object(operations, 'try_post'):
            identity = case.confirm(case.realloc('contention'))['operation_id']
        original = row(runtime, identity)
        source_fields = ('request_id', 'source_json', 'source_digest', 'accepted_at', 'actor')
        with blocked_commit(runtime, module=operations, release_after=2.3) as statements:
            operations._update(runtime.db_path, identity, 'delayed', 'warehouse_busy', {'pending': True})
        assert statements.count('COMMIT') > 1
        # SQLite's trace callback repeats UPDATE text while executing triggers.
        # None of those writes may be replayed after the first COMMIT attempt.
        assert any(sql.startswith('UPDATE') for sql in statements)
        assert not any(sql.startswith('UPDATE') for sql in statements[statements.index('COMMIT') + 1:]), statements
        updated = row(runtime, identity)
        assert updated['state'] == 'delayed' and updated['reason_code'] == 'warehouse_busy'
        assert json.loads(updated['receipt_json']) == {'pending': True}
        assert all(updated[key] == original[key] for key in source_fields)
        with blocked_commit(runtime, module=operations, timeout_ms=350):
            try:
                operations._update(runtime.db_path, identity, 'processing', 'new_pending', {'overwrite': True})
            except SQLiteContentionExhausted as exc:
                assert exc.phase == 'commit'
            else:
                raise AssertionError('persistent reader must not become success')
        assert row(runtime, identity) == updated, 'failed context commit must rollback'
        operations._update(runtime.db_path, identity, 'completed', '', {'proof': 'fixture'})
        completed = row(runtime, identity)
        operations._update(runtime.db_path, identity, 'delayed', 'must_not_replace', {'bad': True})
        assert row(runtime, identity) == completed
    finally:
        case.doCleanups()
    print('operator_warehouse_documents_contention_smoke: OK; COMMIT retry only, bounded rollback, immutable source/completion')


if __name__ == '__main__':
    main()
