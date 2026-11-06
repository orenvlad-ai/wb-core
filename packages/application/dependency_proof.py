"""Transactional dependency revisions for optimistic, detached SQLite proofs.

All persistent tables are instrumented by the explicit installer. A proof records
only tables actually READ, including reads through views, and schema membership.
Consequently direct SQL and old application writers participate automatically.
No reader transaction or writer reservation is kept during expensive Python work.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import sqlite3
import time

CONTRACT = 'sqlite_dependency_proof_v1'
REVISIONS = 'wbc_dependency_revisions'
PREFIX = 'wbc_dependency_revision_'


_SAFE_REASONS = frozenset({
    'dependency_detached_readonly_required','dependency_detached_cursor_bypass_forbidden',
    'dependency_installer_accounting_forbidden','dependency_trigger_contract_changed',
    'dependency_proof_requires_autocommit','dependency_revision_migration_required',
    'dependency_table_uncovered','dependency_proof_already_sealed','dependency_store_not_bound',
    'dependency_file_replaced','dependency_schema_changed','dependency_revision_changed',
    'dependency_proof_budget_exhausted','overhead_completion_book_changed',
    'overhead_completion_authority_changed','overhead_completion_prepare_before_begin_required',
    'overhead_completion_source_changed','overhead_completion_pre_dml_fence_required',
    'overhead_completion_transaction_unbound','overhead_completion_unproven',
    'finance_book_changed', 'finance_book_appeared', 'finance_book_changed_at_handoff',
})
_SAFE_PHASES = frozenset({'proof','planning','validation','writer_handoff','retention','completion'})
_SAFE_CLASSES = frozenset({'relevant_dependency_changed','authority_changed','immutable_source_changed',
    'lock_busy','budget_exhausted','result_unknown','unrelated_or_unclassified_commit'})


class CompletionProofError(ValueError):
    def __init__(self, reason, *, phase='proof', classification='relevant_dependency_changed', business_write_started=False):
        reason = reason if reason in _SAFE_REASONS else 'dependency_proof_failed'
        phase = phase if phase in _SAFE_PHASES else 'proof'
        classification = classification if classification in _SAFE_CLASSES else 'result_unknown'
        super().__init__(reason)
        self.reason, self.phase, self.classification = reason, phase, classification
        self.business_write_started = business_write_started is True
        self.changed_schemas=[]
        self.changed_dependency_count=None
        self.before_vector_digest=None
        self.after_vector_digest=None

    def diagnostic(self):
        return dict(contract_version=CONTRACT, reason=self.reason, phase=self.phase,
                    classification=self.classification, business_write_started=self.business_write_started,
                    result_certainty='unknown' if self.business_write_started else 'not_started',
                    changed_schemas=[s for s in self.changed_schemas if s in {'main','raw','book'}],
                    changed_dependency_count=self.changed_dependency_count if type(self.changed_dependency_count) is int and self.changed_dependency_count>=0 else None,
                    before_vector_digest=self.before_vector_digest if _safe_digest(self.before_vector_digest) else None,
                    after_vector_digest=self.after_vector_digest if _safe_digest(self.after_vector_digest) else None)


def _safe_digest(value):
    return isinstance(value,str) and len(value)==71 and value.startswith('sha256:') and all(c in '0123456789abcdef' for c in value[7:])


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def _trigger(table, event):
    name = PREFIX + hashlib.sha256(table.encode()).hexdigest()[:24] + '_' + event.lower()
    literal = "'" + table.replace("'", "''") + "'"
    sql = (f'CREATE TRIGGER {quote(name)} AFTER {event} ON {quote(table)} BEGIN '
           f'UPDATE {quote(REVISIONS)} SET revision=revision+1 WHERE table_name={literal}; END')
    return name, sql


def install_dependency_revisions(conn):
    """Explicit schema migration; caller owns commit. Never resets a revision.

    This must run on each physical main/raw store, never on accounting. Existing
    triggers must match exactly; a damaged installed contract is not auto-repaired.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables & {'accounting_current', 'accounting_revisions', 'accounting_blobs'}:
        raise CompletionProofError('dependency_installer_accounting_forbidden')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {quote(REVISIONS)} (table_name TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(typeof(revision)=\'integer\' AND revision>=0))')
    installed = {r[0] for r in conn.execute(f'SELECT table_name FROM {quote(REVISIONS)}')}
    triggers = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    for table in sorted(tables - {REVISIONS}):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            name, sql = _trigger(table, event)
            if table in installed:
                if triggers.get(name) != sql:
                    raise CompletionProofError('dependency_trigger_contract_changed')
            else:
                conn.execute(sql)
        conn.execute(f'INSERT OR IGNORE INTO {quote(REVISIONS)} VALUES (?,0)', (table,))


class _DetachedCursor:
    def __init__(self, cursor):
        self.description=cursor.description
        self.rowcount=cursor.rowcount
        self.lastrowid=cursor.lastrowid
        self.rows=cursor.fetchall()
        self.offset=0
        cursor.close()

    def fetchone(self):
        if self.offset>=len(self.rows): return None
        row=self.rows[self.offset];self.offset+=1
        return row

    def fetchall(self):
        rows=self.rows[self.offset:];self.offset=len(self.rows)
        return rows

    def fetchmany(self, size=1):
        rows=self.rows[self.offset:self.offset+size];self.offset+=len(rows)
        return rows

    def __iter__(self):
        while self.offset<len(self.rows):
            yield self.fetchone()


class DetachedReadConnection:
    """Release each SQL statement's DELETE reader before caller Python work.

    SQL execution/fetch is still bounded by SQLite's progress handler. Revision
    vectors close changes between detached statements; this is not a transaction.
    """
    def __init__(self, connection):
        if connection.in_transaction or connection.execute('PRAGMA query_only').fetchone()[0]!=1:
            raise CompletionProofError('dependency_detached_readonly_required')
        self.connection=connection

    def execute(self, sql, parameters=()):
        cursor=self.connection.execute(sql,parameters)
        return _DetachedCursor(cursor) if cursor.description is not None else cursor

    def cursor(self, *args, **kwargs):
        raise CompletionProofError('dependency_detached_cursor_bypass_forbidden')

    def __getattr__(self, key):
        return getattr(self.connection,key)


class DependencyProof:
    def __init__(self, conn):
        if conn.in_transaction:
            raise CompletionProofError('dependency_proof_requires_autocommit')
        self.conn = conn
        self.reads = {}
        self.stores = {}
        self.sealed = False
        for _, schema, filename in conn.execute('PRAGMA database_list'):
            if schema == 'temp':
                continue
            path = Path(filename).resolve(strict=True)
            stat = path.stat()
            schema_version = conn.execute(f'PRAGMA {quote(schema)}.schema_version').fetchone()[0]
            master = list(conn.execute(f'SELECT type,name,sql FROM {quote(schema)}.sqlite_master'))
            tables = {r[1] for r in master if r[0] == 'table' and not r[1].startswith('sqlite_')}
            if REVISIONS not in tables:
                raise CompletionProofError('dependency_revision_migration_required')
            revisions = dict(conn.execute(f'SELECT table_name,revision FROM {quote(schema)}.{quote(REVISIONS)}'))
            triggers = {r[1]:r[2] for r in master if r[0] == 'trigger'}
            for table in tables - {REVISIONS}:
                if table not in revisions:
                    raise CompletionProofError('dependency_table_uncovered')
                for event in ('INSERT', 'UPDATE', 'DELETE'):
                    name, sql = _trigger(table, event)
                    if triggers.get(name) != sql:
                        raise CompletionProofError('dependency_trigger_contract_changed')
            self.stores[schema] = dict(path=str(path), device=stat.st_dev, inode=stat.st_ino,
                                      schema_version=schema_version, revisions=revisions)
        self.assert_current(conn)

    @classmethod
    def capture(cls, conn):
        return cls(conn)

    @contextmanager
    def observe(self):
        if self.sealed:
            raise CompletionProofError('dependency_proof_already_sealed')
        def authorizer(action, table, column, schema, source):
            # Built-in JSON table functions read only their already-observed input.
            if action == sqlite3.SQLITE_READ and table in ('json_each','json_tree') and table not in self.stores.get(schema,{}).get('revisions',{}):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and schema != 'temp' and table not in ('sqlite_master', 'sqlite_schema'):
                if schema not in self.stores or table not in self.stores[schema]['revisions'] or table == REVISIONS:
                    return sqlite3.SQLITE_DENY
                self.reads.setdefault((schema, table), set()).add(column)
            return sqlite3.SQLITE_OK
        self.conn.set_authorizer(authorizer)
        try:
            yield self
        finally:
            self.conn.set_authorizer(None)

    def seal(self):
        self.assert_current(self.conn)
        self.sealed = True
        return self.receipt()

    def assert_current(self, conn):
        """Fresh short check. Caller must reserve all stores until its commit.

        Names may differ on a writer; physical path identities bind the mapping.
        There is deliberately no 'ignore tables' or baseline reset operation.
        """
        attached = {str(Path(r[2]).resolve()): r[1] for r in conn.execute('PRAGMA database_list') if r[1] != 'temp'}
        for original, store in self.stores.items():
            schema = attached.get(store['path'])
            if schema is None:
                raise CompletionProofError('dependency_store_not_bound', classification='authority_changed')
            stat = Path(store['path']).stat()
            if (stat.st_dev, stat.st_ino) != (store['device'], store['inode']):
                raise CompletionProofError('dependency_file_replaced', classification='authority_changed')
            if conn.execute(f'PRAGMA {quote(schema)}.schema_version').fetchone()[0] != store['schema_version']:
                raise CompletionProofError('dependency_schema_changed', classification='authority_changed')
            selected = [table for name, table in self.reads if name == original]
            if not selected:
                continue
            values = dict(conn.execute(f'SELECT table_name,revision FROM {quote(schema)}.{quote(REVISIONS)} WHERE table_name IN ({",".join("?" for _ in selected)})', selected))
            changed=[table for table in selected if values.get(table)!=store['revisions'][table]]
            if changed:
                error=CompletionProofError('dependency_revision_changed')
                error.changed_schemas=['main' if original=='main' else 'raw']
                error.changed_dependency_count=len(changed)
                def digest(values):
                    return 'sha256:'+hashlib.sha256(json.dumps([[t,values.get(t)] for t in sorted(selected)],separators=(',',':')).encode()).hexdigest()
                error.before_vector_digest=digest(store['revisions'])
                error.after_vector_digest=digest(values)
                raise error

    def receipt(self):
        vector = [dict(schema=schema, table=table, columns=sorted(columns),
                       revision=self.stores[schema]['revisions'][table])
                  for (schema, table), columns in sorted(self.reads.items())]
        body = dict(contract_version=CONTRACT, dependencies=vector,
                    stores={k:{x:v for x,v in s.items() if x != 'revisions'} for k,s in self.stores.items()})
        body['digest'] = 'sha256:' + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return body


_DEADLINE = ContextVar('dependency_proof_deadline', default=None)


def check_deadline():
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise CompletionProofError('dependency_proof_budget_exhausted', classification='budget_exhausted')


@contextmanager
def readonly_deadline(conn, deadline):
    """One cooperative SQL/Python budget; never wrap a business transaction.

    Native loops call check_deadline between bounded units. A single C JSON/hash
    call is indivisible; this API makes no hard real-time guarantee for it.
    """
    token = _DEADLINE.set(deadline)
    def progress():
        return int(deadline is not None and time.monotonic() >= deadline)
    if conn is not None: conn.set_progress_handler(progress, 1000)
    try:
        check_deadline()
        yield
        check_deadline()
    except sqlite3.OperationalError as exc:
        if getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT:
            check_deadline()
        raise
    finally:
        if conn is not None: conn.set_progress_handler(None, 0)
        _DEADLINE.reset(token)
