"""Known sku_groups revision-trigger upgrade; disposable databases only."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import ready_publication as publication
from packages.application.registry_upload_db_backed_runtime import _ensure_schema
from packages.application.web_vitrina_history_live_adapter import (
    LiveSourceUnavailable, _verify_revision_triggers,
)

TABLE = 'sheet_vitrina_v1_sku_groups'
TRIGGER = 'ready_input_' + TABLE + '_update'


def fixture(conn, *, already_upgraded):
    conn.execute('''CREATE TABLE sheet_vitrina_v1_sku_groups (
        group_key TEXT PRIMARY KEY, label TEXT NOT NULL,
        aliases_json TEXT NOT NULL DEFAULT '[]', is_active INTEGER NOT NULL DEFAULT 1,
        is_system INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    publication.ensure_material_revisions(conn)
    # This is the existing producer's exact seven-column trigger, before the
    # display_order ALTER. The upgrade must not treat this table as fresh.
    old = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()[0]
    assert 'display_order' not in old
    conn.execute(f"INSERT INTO {TABLE} VALUES('clean','Clean','[]',1,1,'before','before')")
    if already_upgraded:
        conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0')
    conn.commit()
    return old


def revision(conn):
    return conn.execute(f'SELECT revision FROM {publication.REVISIONS} WHERE source_table=?', (TABLE,)).fetchone()[0]


def strict(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _verify_revision_triggers(conn, tables)


def assert_error(fn, error, reason):
    try:
        fn()
    except error as exc:
        assert reason in str(exc), str(exc)
    else:
        raise AssertionError('expected refusal: ' + reason)


def startup(path, *, already_upgraded):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        old = fixture(conn, already_upgraded=already_upgraded)
        before = revision(conn)
        if already_upgraded:
            assert not publication.material_revisions_schema_ready(conn)
            assert_error(lambda: strict(conn), LiveSourceUnavailable, TRIGGER)
        # Real runtime startup, including both ADD COLUMN and the released
        # database that already has display_order but retained the old trigger.
        _ensure_schema(conn)
        assert publication.material_revisions_schema_ready(conn)
        strict(conn)
        updated = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()[0]
        assert updated != old and 'display_order' in updated
        assert revision(conn) == before
        conn.execute(f"UPDATE {TABLE} SET display_order=7 WHERE group_key='clean'")
        conn.commit()
        assert revision(conn) == before + 1
        conn.execute(f"UPDATE {TABLE} SET display_order=display_order WHERE group_key='clean'")
        conn.commit()
        assert revision(conn) == before + 1
        assert tuple(conn.execute(f'SELECT label,updated_at,display_order FROM {TABLE}').fetchone()) == ('Clean','before',7)
        dump = list(conn.iterdump())
        publication.ensure_material_revisions(conn)
        _ensure_schema(conn)
        assert list(conn.iterdump()) == dump
        strict(conn)


def unknown(path):
    with closing(sqlite3.connect(path)) as conn:
        fixture(conn, already_upgraded=True)
        conn.execute(f'DROP TRIGGER "{TRIGGER}"')
        conn.execute(f'''CREATE TRIGGER "{TRIGGER}" AFTER UPDATE ON "{TABLE}"
            BEGIN UPDATE {publication.REVISIONS} SET revision=revision+2 WHERE source_table='{TABLE}'; END''')
        conn.commit()
        before = path.read_bytes()
        sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone()[0]
        for ensure in (publication.ensure_material_revisions, _ensure_schema):
            assert_error(lambda: ensure(conn), ValueError, 'ready_source_revision_trigger_unknown:' + TRIGGER)
            assert path.read_bytes() == before
            assert conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone()[0] == sql
        assert_error(lambda: strict(conn), LiveSourceUnavailable, TRIGGER)


def owner_transaction(path):
    with closing(sqlite3.connect(path)) as conn:
        old = fixture(conn, already_upgraded=True)
        conn.execute('BEGIN')
        publication.ensure_material_revisions(conn)
        assert conn.in_transaction
        strict(conn)
        conn.rollback()
        assert conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone()[0] == old
        assert_error(lambda: strict(conn), LiveSourceUnavailable, TRIGGER)
        # A failed CREATE must also roll back the DROP, independently of the
        # caller deciding whether to retain its surrounding transaction.
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK)
        assert_error(lambda: publication.ensure_material_revisions(conn), sqlite3.DatabaseError, 'not authorized')
        conn.set_authorizer(None)
        assert conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone()[0] == old


def main():
    with TemporaryDirectory(prefix='group-trigger-upgrade-') as tmp:
        root = Path(tmp)
        startup(root/'legacy.sqlite3', already_upgraded=False)
        startup(root/'upgraded.sqlite3', already_upgraded=True)
        unknown(root/'unknown.sqlite3')
        owner_transaction(root/'transaction.sqlite3')
    print(json.dumps({'status':'PASS','existing_old_schema_startup':True,
        'existing_upgraded_schema_old_trigger_startup':True,'strict_RO_guard_unchanged':True,
        'display_order_revision':True,'no_op_idempotent':True,'unknown_SQL_DB_unchanged':True,
        'owner_transaction_and_DDL_rollback':True}))


if __name__ == '__main__':
    main()
