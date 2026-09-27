#!/usr/bin/env python3
"""Explicit, offline upgrade of the two populated registry CHECK tables.

Dry-run is read-only. Apply requires the reviewed plan digest and an external
backup; neither a cleaner GET nor process startup invokes this migration.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.change_registry import ensure_change_registry_schema
from packages.application.change_registry_search_cluster import needs_bidirectional_upgrade,needs_schema_migration,_row_digest
from packages.application.storage_registry import StoreRegistry

TABLES=('change_registry_items','change_registry_facts')


def plan(conn,registry):
    conn.execute('PRAGMA query_only=ON')
    owns_snapshot=not conn.in_transaction
    if owns_snapshot:conn.execute('BEGIN')
    try:
        manifest=registry.load();file=registry.resolve('operational',manifest=manifest)
        opened=[row[2] for row in conn.execute('PRAGMA database_list') if row[1]=='main']
        if len(opened)!=1 or not opened[0] or not file.is_file() or not os.path.samefile(opened[0],file):
            raise RuntimeError('open database does not match current operational generation')
        if needs_schema_migration(conn):raise RuntimeError('legacy query identity migration must be handled separately')
        tables=[]
        for table in TABLES:
            row=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if not row:raise RuntimeError('registry table missing: '+table)
            count=conn.execute('SELECT count(*) FROM '+table).fetchone()[0]
            row_bytes=sum(len(json.dumps(tuple(item),ensure_ascii=False,separators=(',',':'),default=str).encode('utf-8'))+1
                          for item in conn.execute(f'SELECT * FROM {table} ORDER BY rowid'))
            try:bytes_used=conn.execute('SELECT sum(pgsize) FROM dbstat WHERE name=?',(table,)).fetchone()[0]
            except Exception:bytes_used=None
            tables.append(dict(name=table,rows=count,row_sha256=_row_digest(conn,table)[1],
                               ddl_sha256=hashlib.sha256(row[0].encode()).hexdigest(),table_bytes=bytes_used,
                               row_json_bytes=row_bytes))
        objects=[tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN('trigger','index') AND sql IS NOT NULL ORDER BY type,name")]
        object_digest=hashlib.sha256(json.dumps(objects,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        stat=file.stat()
        if registry.load().manifest_sha256!=manifest.manifest_sha256:
            raise RuntimeError('operational generation changed during registry plan')
        status=dict(contract='search-cluster-registry-bidirectional-v1',needed=needs_bidirectional_upgrade(conn),tables=tables,
                    schema_objects_sha256=object_digest,schema_objects=len(objects),manifest_sha256=manifest.manifest_sha256,
                    operational_generation=manifest.operational.generation_id,file_identity=[stat.st_dev,stat.st_ino,stat.st_size],
                    estimated_journal_bytes=sum(item['row_json_bytes'] for item in tables)+len(json.dumps(objects,ensure_ascii=False).encode()))
        status['plan_sha256']=hashlib.sha256(json.dumps(status,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return status
    finally:
        if owns_snapshot:conn.rollback()


def write_journal(path,registry,before):
    """Fsynced 0600 rollback evidence; never stores unrelated business rows."""
    path=path.expanduser().resolve()
    if not path.parent.is_dir():raise RuntimeError('journal parent directory missing')
    with registry.session('operational',mode='ro',operation='cleaner_registry_upgrade_journal') as conn:
        conn.execute('BEGIN')
        try:
            if plan(conn,registry)['plan_sha256']!=before['plan_sha256']:raise RuntimeError('plan changed before journal')
            tables={}
            for table in TABLES:
                ddl=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
                tables[table]=dict(create_table_sql=ddl,columns=[row[1] for row in conn.execute(f'PRAGMA table_info({table})')],
                                   rows=[list(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')],
                                   foreign_keys=[list(row) for row in conn.execute(f'PRAGMA foreign_key_list({table})')])
            objects=[list(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN('trigger','index') AND sql IS NOT NULL ORDER BY type,name")]
            document=dict(contract='search-cluster-registry-upgrade-journal-v1',plan=before,tables=tables,objects=objects)
        finally:conn.rollback()
    encoded=json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(descriptor,'wb') as stream:stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        parent_fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(parent_fd)
        finally:os.close(parent_fd)
    except BaseException:
        try:path.unlink()
        except OSError:pass
        raise
    return hashlib.sha256(encoded).hexdigest()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir',required=True,type=Path)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--expected-plan-sha256',default='')
    parser.add_argument('--journal-path',type=Path)
    args=parser.parse_args(argv)
    registry=StoreRegistry(args.runtime_dir)
    with registry.session('operational',mode='ro',operation='cleaner_registry_upgrade_plan') as conn:
        before=plan(conn,registry)
    if not args.apply:
        print(json.dumps(before,sort_keys=True));return 0
    if args.expected_plan_sha256!=before['plan_sha256']:
        raise RuntimeError('reviewed registry plan changed')
    if not before['needed']:
        print(json.dumps(dict(state='already_upgraded',plan_sha256=before['plan_sha256']),sort_keys=True));return 0
    if not args.journal_path:raise RuntimeError('an external fsynced 0600 journal path is required')
    journal_sha=write_journal(args.journal_path,registry,before)
    with registry.session('operational',mode='rw',operation='cleaner_registry_bidirectional_upgrade',timeout_ms=1000) as conn:
        if plan(conn,registry)['plan_sha256']!=before['plan_sha256']:raise RuntimeError('registry plan changed before write')
        conn.execute('PRAGMA query_only=OFF')
        ensure_change_registry_schema(conn,upgrade_bidirectional=True,reviewed_bidirectional_plan=before)
        conn.commit()
        if needs_bidirectional_upgrade(conn):raise RuntimeError('registry upgrade did not install both CHECK constraints')
        if conn.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('registry foreign key check failed; restore reviewed backup')
        after=plan(conn,registry)
    if (any(old['rows']!=new['rows'] or old['row_sha256']!=new['row_sha256'] for old,new in zip(before['tables'],after['tables']))
            or before['schema_objects_sha256']!=after['schema_objects_sha256']):
        raise RuntimeError('registry rows or unrelated schema changed; restore reviewed backup')
    print(json.dumps(dict(state='upgraded',before_plan_sha256=before['plan_sha256'],journal_sha256=journal_sha,after=after),sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
