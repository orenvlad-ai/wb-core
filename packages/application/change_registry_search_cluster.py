"""Narrow cleaner extension: same operational transaction, exact query evidence.

Legacy rows, identities and observer acquisition are deliberately unchanged.
The only rebuilt tables are items/facts; their previous columns are copied verbatim.
"""
from __future__ import annotations
import hashlib
import re
import sqlite3
from datetime import datetime, timezone

DOMAIN_TABLE = 'change_registry_search_cluster_queries'
EVIDENCE_TABLE = 'change_registry_search_cluster_readbacks'


def migrate_search_cluster_schema(conn, schema_sql):
    from packages.application.change_registry import ITEMS_TABLE, FACTS_TABLE
    tables = [t for t in (ITEMS_TABLE, FACTS_TABLE)
              if conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(t,)).fetchone()
              and 'query_hash' not in {r[1] for r in conn.execute(f'PRAGMA table_info({t})')}]
    if tables:
        if conn.in_transaction:
            raise RuntimeError('registry migration requires explicit setup outside a business transaction')
        foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
        conn.execute('PRAGMA foreign_keys=OFF')
        try:
            conn.execute('BEGIN IMMEDIATE')
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
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('registry migration foreign key mismatch')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
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


def _time(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError('aware registry timestamp required')
    return result.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def _insert(conn, table, values):
    conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))


def _id(prefix,*values):
    from packages.application.change_registry import canonical_json
    return prefix+hashlib.sha256(canonical_json(values).encode()).hexdigest()


def prepare_in_transaction(conn, *, operation_id, account, target, queries, created_at, before_at,
                           provenance, actor='cleaner', source='automatic'):
    from packages.application.change_registry import canonical_digest, MAPPING_VERSION, target_identity
    if not conn.in_transaction: raise RuntimeError('atomic caller transaction required')
    if not queries or len(queries)!=len(set(queries)): raise ValueError('unique nonempty additions required')
    now=_time(created_at)
    _insert(conn,'change_registry_operations',dict(operation_id=operation_id,seller_id=account.seller_id,
        account_scope=account.account_scope,source_surface='search_cluster_cleaner',actor_principal=actor,
        actor_kind='human' if source=='owner_decision' else 'service',requested_at=now,created_at=now,
        native_idempotency_key=operation_id,correlation_id=operation_id,provenance_digest=canonical_digest(provenance),mapping_version=MAPPING_VERSION))
    result={}
    for query in queries:
        from packages.contracts.search_cluster_cleaner import query_hash
        qh=query_hash(query);target_identity('search_cluster',nm_id=target.nm_id,advert_id=target.advert_id,query_hash=qh)
        existing=conn.execute(f'SELECT query FROM {DOMAIN_TABLE} WHERE seller_id=? AND account_scope=? AND advert_id=? AND nm_id=? AND query_hash=?',
                             (account.seller_id,account.account_scope,target.advert_id,target.nm_id,qh)).fetchone()
        if existing and existing[0]!=query: raise ValueError('exact query identity conflict')
        if not existing:
            _insert(conn,DOMAIN_TABLE,dict(seller_id=account.seller_id,account_scope=account.account_scope,advert_id=target.advert_id,nm_id=target.nm_id,query_hash=qh,query=query))
        item=_id('crci_',operation_id,qh)
        _insert(conn,'change_registry_items',dict(change_item_id=item,operation_id=operation_id,seller_id=account.seller_id,
            account_scope=account.account_scope,target_kind='search_cluster',nm_id=target.nm_id,advert_id=target.advert_id,
            query_hash=qh,parameter_field='excluded',before_value_kind='boolean',before_value_integer=0,
            requested_value_kind='boolean',requested_value_integer=1,mapping_version=MAPPING_VERSION,created_at=now))
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
        if not domain or domain[0] not in present_queries: continue
        fact=_id('crcf_',item['change_item_id'])
        if not conn.execute('SELECT 1 FROM change_registry_facts WHERE fact_id=?',(fact,)).fetchone():
            _insert(conn,'change_registry_facts',dict(fact_id=fact,seller_id=item['seller_id'],account_scope=item['account_scope'],
                target_kind='search_cluster',nm_id=item['nm_id'],advert_id=item['advert_id'],query_hash=item['query_hash'],parameter_field='excluded',
                before_value_kind='boolean',before_value_integer=0,after_value_kind='boolean',after_value_integer=1,
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
