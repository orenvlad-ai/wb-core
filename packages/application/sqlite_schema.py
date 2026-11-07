"""Idempotent installation of schema-owner-managed views and triggers."""
import re
import sqlite3


def ensure_schema_definition(conn: sqlite3.Connection, *, kind: str, name: str, sql: str) -> None:
    """Keep an exact current definition; replace an older one atomically.

    Do not normalize SQL bodies: whitespace inside literals can be semantic.
    SQLite omits the statement's outer whitespace and trailing semicolon in
    sqlite_master. A real definition change must still advance schema_version
    so every previously captured dependency proof fails closed.
    """
    if kind not in {"view", "trigger"} or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise ValueError("invalid managed SQLite schema object")
    expected = sql.strip().removesuffix(";")

    def current():
        row = conn.execute("SELECT type,sql FROM main.sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and row[0] != kind:
            raise ValueError("managed SQLite schema object type changed")
        return row

    existing = current()
    if existing is not None and existing[1] == expected:
        return
    # The savepoint also nests safely inside a caller-owned transaction. The
    # old guard/view remains installed if CREATE fails after DROP.
    conn.execute("SAVEPOINT ensure_schema_definition")
    try:
        existing = current()
        if existing is None or existing[1] != expected:
            if existing is not None:
                conn.execute(f'DROP {kind.upper()} main."{name}"')
            conn.execute(expected)
        conn.execute("RELEASE SAVEPOINT ensure_schema_definition")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT ensure_schema_definition")
        conn.execute("RELEASE SAVEPOINT ensure_schema_definition")
        raise
