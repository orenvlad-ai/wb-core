"""Narrow cleaner extension: same operational transaction, exact query evidence.

Legacy rows, identities and observer acquisition are deliberately unchanged.
The only rebuilt tables are items/facts; their previous columns are copied verbatim.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

DOMAIN_TABLE = 'change_registry_search_cluster_queries'
EVIDENCE_TABLE = 'change_registry_search_cluster_readbacks'


def needs_schema_migration(conn) -> bool:
    """Whether legacy registry items/facts lack the query identity column."""
    from packages.application.change_registry import ITEMS_TABLE, FACTS_TABLE
    return any(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        and 'query_hash' not in {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        for table in (ITEMS_TABLE, FACTS_TABLE)
    )


def needs_bidirectional_upgrade(conn) -> bool:
    """Read-only proof that the two generic tables still reject excluded 1→0."""
    from packages.application.change_registry import ITEMS_TABLE, FACTS_TABLE
    for table,after in ((ITEMS_TABLE,'requested_value'),(FACTS_TABLE,'after_value')):
        row=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
        if not row:continue
        ddl=row[0]
        if not all(re.search(rf"parameter_field='excluded'\s+AND\s+{field}_kind='boolean'\s+AND\s+{field}_integer\s+IN\s*\(\s*0\s*,\s*1\s*\)",ddl)
                   for field in ('before_value',after)):
            return True
    return False


def bidirectional_ready(conn) -> bool:
    """Read-only readiness for manual intake and the durable worker queue."""
    from packages.application.change_registry import ITEMS_TABLE, FACTS_TABLE
    return all(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
               for table in (ITEMS_TABLE,FACTS_TABLE)) and not needs_bidirectional_upgrade(conn)


def migrate_search_cluster_schema(conn, schema_sql, *, upgrade_bidirectional=False, reviewed_plan=None):
    from packages.application.change_registry import ITEMS_TABLE, FACTS_TABLE
    tables = [t for t in (ITEMS_TABLE, FACTS_TABLE)
              if conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(t,)).fetchone()
              and ('query_hash' not in {r[1] for r in conn.execute(f'PRAGMA table_info({t})')}
                   or upgrade_bidirectional and _table_needs_bidirectional(conn,t))]
    if tables:
        if conn.in_transaction:
            raise RuntimeError('registry migration requires explicit setup outside a business transaction')
        foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
        legacy_alter_table = conn.execute('PRAGMA legacy_alter_table').fetchone()[0]
        conn.execute('PRAGMA foreign_keys=OFF')
        # ALTER TABLE RENAME otherwise reparses every view in the database.
        # Keep unrelated, pre-existing invalid views untouched during this
        # atomic replacement of the registry tables, then restore the caller's
        # connection setting below.
        conn.execute('PRAGMA legacy_alter_table=ON')
        try:
            conn.execute('BEGIN IMMEDIATE')
            original_objects=[tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN('trigger','index') AND sql IS NOT NULL ORDER BY type,name")]
            if reviewed_plan is not None:
                if not upgrade_bidirectional or [item['name'] for item in reviewed_plan.get('tables',[])]!=list((ITEMS_TABLE,FACTS_TABLE)):
                    raise RuntimeError('reviewed registry upgrade scope mismatch')
                objects_hash=hashlib.sha256(json.dumps(original_objects,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                if objects_hash!=reviewed_plan['schema_objects_sha256']:
                    raise RuntimeError('reviewed registry schema changed before upgrade')
                for item in reviewed_plan['tables']:
                    ddl=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(item['name'],)).fetchone()
                    if (not ddl or hashlib.sha256(ddl[0].encode()).hexdigest()!=item['ddl_sha256']
                            or _row_digest(conn,item['name'])!=(item['rows'],item['row_sha256'])):
                        raise RuntimeError('reviewed registry rows changed before upgrade')
            original_rows={table:_row_digest(conn,table) for table in tables} if upgrade_bidirectional else {}
            # Temporarily remove and restore exact trigger definitions, including
            # triggers on other tables that reference a rebuilt parent.
            triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
            indexes=conn.execute("SELECT name,sql,tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()
            for name,_ in triggers: conn.execute(f'DROP TRIGGER "{name}"')
            for table in tables:
                ddl=re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\([\s\S]*?\n        \);',schema_sql).group(0)
                conn.execute(ddl.replace(table+'(',table+'_new(',1))
                columns=','.join('"'+r[1]+'"' for r in conn.execute(f'PRAGMA table_info({table})'))
                conn.execute(f'INSERT INTO {table}_new({columns}) SELECT {columns} FROM {table}')
                conn.execute(f'DROP TABLE {table}')
                conn.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
            for _,ddl,table in indexes:
                if table in tables: conn.execute(ddl)
            for _,ddl in triggers: conn.execute(ddl)
            if upgrade_bidirectional:
                restored=[tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN('trigger','index') AND sql IS NOT NULL ORDER BY type,name")]
                if restored!=original_objects or any(_row_digest(conn,table)!=value for table,value in original_rows.items()):
                    raise RuntimeError('registry upgrade changed unrelated schema or immutable rows')
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('registry migration foreign key mismatch')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute(f'PRAGMA legacy_alter_table={int(legacy_alter_table)}')
            conn.execute(f'PRAGMA foreign_keys={int(foreign_keys)}')
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {DOMAIN_TABLE}(
          seller_id TEXT NOT NULL,account_scope TEXT NOT NULL,advert_id INTEGER NOT NULL,nm_id INTEGER NOT NULL,
          query_hash TEXT NOT NULL CHECK(length(query_hash)=64 AND query_hash NOT GLOB '*[^0-9a-f]*'),
          query TEXT NOT NULL CHECK(length(query)>0),
          PRIMARY KEY(seller_id,account_scope,advert_id,nm_id,query_hash));
        CREATE TABLE IF NOT EXISTS {EVIDENCE_TABLE}(
          evidence_id TEXT PRIMARY KEY,change_item_id TEXT NOT NULL REFERENCES change_registry_items(change_item_id),
          fact_id TEXT REFERENCES change_registry_facts(fact_id),observed_at TEXT NOT NULL,
          evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),evidence_digest TEXT NOT NULL,
          UNIQUE(change_item_id,observed_at,evidence_digest));
    """)
    for table in (DOMAIN_TABLE,EVIDENCE_TABLE):
        for op in ('UPDATE','DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable cluster evidence'); END")


def _table_needs_bidirectional(conn,table):
    row=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
    if not row:return False
    after='requested_value' if table.endswith('_items') else 'after_value'
    return not all(re.search(rf"parameter_field='excluded'\s+AND\s+{field}_kind='boolean'\s+AND\s+{field}_integer\s+IN\s*\(\s*0\s*,\s*1\s*\)",row[0])
                   for field in ('before_value',after))


def _row_digest(conn,table):
    value=hashlib.sha256();count=0
    for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid'):
        value.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':'),default=str).encode('utf-8'))
        value.update(b'\n');count+=1
    return count,value.hexdigest()


def _time(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError('aware registry timestamp required')
    return result.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def _insert(conn, table, values):
    conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))


def _id(prefix,*values):
    from packages.application.change_registry import canonical_json
    return prefix+hashlib.sha256(canonical_json(values).encode()).hexdigest()


def prepare_in_transaction(conn, *, operation_id, account, target, queries, returns=(), created_at, before_at,
                           provenance, actor='cleaner', source='automatic'):
    from packages.application.change_registry import canonical_digest, MAPPING_VERSION, target_identity
    if not conn.in_transaction: raise RuntimeError('atomic caller transaction required')
    if (not (queries or returns) or len(queries)!=len(set(queries)) or len(returns)!=len(set(returns))
            or set(queries)&set(returns)): raise ValueError('unique disjoint full-set changes required')
    if needs_bidirectional_upgrade(conn):raise ValueError('search cluster registry needs governed bidirectional upgrade')
    now=_time(created_at)
    _insert(conn,'change_registry_operations',dict(operation_id=operation_id,seller_id=account.seller_id,
        account_scope=account.account_scope,source_surface='search_cluster_cleaner',actor_principal=actor,
        actor_kind='human' if source in {'owner_decision','manual_rules_pilot'} else 'service',requested_at=now,created_at=now,
        native_idempotency_key=operation_id,correlation_id=operation_id,provenance_digest=canonical_digest(provenance),mapping_version=MAPPING_VERSION))
    result={}
    for query in list(queries)+list(returns):
        from packages.contracts.search_cluster_cleaner import query_hash
        qh=query_hash(query);target_identity('search_cluster',nm_id=target.nm_id,advert_id=target.advert_id,query_hash=qh)
        existing=conn.execute(f'SELECT query FROM {DOMAIN_TABLE} WHERE seller_id=? AND account_scope=? AND advert_id=? AND nm_id=? AND query_hash=?',
                             (account.seller_id,account.account_scope,target.advert_id,target.nm_id,qh)).fetchone()
        if existing and existing[0]!=query: raise ValueError('exact query identity conflict')
        if not existing:
            _insert(conn,DOMAIN_TABLE,dict(seller_id=account.seller_id,account_scope=account.account_scope,advert_id=target.advert_id,nm_id=target.nm_id,query_hash=qh,query=query))
        item=_id('crci_',operation_id,qh)
        is_return=query in returns
        _insert(conn,'change_registry_items',dict(change_item_id=item,operation_id=operation_id,seller_id=account.seller_id,
            account_scope=account.account_scope,target_kind='search_cluster',nm_id=target.nm_id,advert_id=target.advert_id,
            query_hash=qh,parameter_field='excluded',before_value_kind='boolean',before_value_integer=int(is_return),
            requested_value_kind='boolean',requested_value_integer=int(not is_return),mapping_version=MAPPING_VERSION,created_at=now))
        attempt=_id('crca_',item)
        for sequence,state in [(1,'created'),(2,'submitted')]:
            _insert(conn,'change_registry_attempt_events',dict(attempt_event_id=_id('crce_',attempt,sequence),attempt_id=attempt,
                change_item_id=item,sequence_no=sequence,state=state,occurred_at=now,native_event_key='cleaner_'+state))
        result[qh]=item
    return result


def confirm_in_transaction(conn, *, operation_id, present_queries, observed_at, before_at, evidence):
    from packages.application.change_registry import canonical_digest, canonical_json, MAPPING_VERSION
    if not conn.in_transaction: raise RuntimeError('atomic caller transaction required')
    now=_time(observed_at);evidence_digest=canonical_digest(evidence);result={}
    for item in conn.execute('SELECT * FROM change_registry_items WHERE operation_id=? AND target_kind=\'search_cluster\'',(operation_id,)).fetchall():
        domain=conn.execute(f'SELECT query FROM {DOMAIN_TABLE} WHERE seller_id=? AND account_scope=? AND advert_id=? AND nm_id=? AND query_hash=?',
            (item['seller_id'],item['account_scope'],item['advert_id'],item['nm_id'],item['query_hash'])).fetchone()
        desired=item['requested_value_integer']
        if not domain or (domain[0] in present_queries)!=bool(desired): continue
        fact=_id('crcf_',item['change_item_id'])
        if not conn.execute('SELECT 1 FROM change_registry_facts WHERE fact_id=?',(fact,)).fetchone():
            _insert(conn,'change_registry_facts',dict(fact_id=fact,seller_id=item['seller_id'],account_scope=item['account_scope'],
                target_kind='search_cluster',nm_id=item['nm_id'],advert_id=item['advert_id'],query_hash=item['query_hash'],parameter_field='excluded',
                before_value_kind='boolean',before_value_integer=item['before_value_integer'],after_value_kind='boolean',after_value_integer=desired,
                observed_from=_time(before_at),observed_to=now,proven_at=now,proof_kind='wb_readback',evidence_digest=evidence_digest,mapping_version=MAPPING_VERSION))
            _insert(conn,'change_registry_fact_links',dict(fact_link_id=_id('crcl_',fact),fact_id=fact,link_kind='change_item',change_item_id=item['change_item_id'],linked_at=now,evidence_digest=evidence_digest))
            attempt=_id('crca_',item['change_item_id'])
            _insert(conn,'change_registry_attempt_events',dict(attempt_event_id=_id('crce_',attempt,3),attempt_id=attempt,
                change_item_id=item['change_item_id'],sequence_no=3,state='confirmed',occurred_at=now,
                readback_proof_kind='wb_readback',readback_digest=evidence_digest,native_event_key='cleaner_confirmed'))
        eid=_id('crcb_',item['change_item_id'],now,evidence_digest)
        if not conn.execute(f'SELECT 1 FROM {EVIDENCE_TABLE} WHERE evidence_id=?',(eid,)).fetchone():
            _insert(conn,EVIDENCE_TABLE,dict(evidence_id=eid,change_item_id=item['change_item_id'],fact_id=fact,
                observed_at=now,evidence_json=canonical_json(evidence),evidence_digest=evidence_digest))
        result[item['query_hash']]=fact
    return result


def reject_in_transaction(conn, *, operation_id, observed_at, evidence):
    """Close an explicitly rejected full set without manufacturing facts."""
    from packages.application.change_registry import canonical_digest, canonical_json
    if not conn.in_transaction: raise RuntimeError('atomic caller transaction required')
    now=_time(observed_at);evidence_digest=canonical_digest(evidence)
    for item in conn.execute("SELECT * FROM change_registry_items WHERE operation_id=? AND target_kind='search_cluster'",(operation_id,)).fetchall():
        attempt=_id('crca_',item['change_item_id'])
        existing=conn.execute('SELECT state FROM change_registry_attempt_events WHERE attempt_id=? AND sequence_no=3',(attempt,)).fetchone()
        if existing is None:
            _insert(conn,'change_registry_attempt_events',dict(attempt_event_id=_id('crce_',attempt,3),attempt_id=attempt,
                change_item_id=item['change_item_id'],sequence_no=3,state='rejected',occurred_at=now,native_event_key='cleaner_validation_rejected'))
        elif existing['state']!='rejected':
            raise ValueError('search cluster operation already has a different terminal receipt')
        eid=_id('crcb_',item['change_item_id'],now,evidence_digest)
        if not conn.execute(f'SELECT 1 FROM {EVIDENCE_TABLE} WHERE evidence_id=?',(eid,)).fetchone():
            _insert(conn,EVIDENCE_TABLE,dict(evidence_id=eid,change_item_id=item['change_item_id'],fact_id=None,
                observed_at=now,evidence_json=canonical_json(evidence),evidence_digest=evidence_digest))
