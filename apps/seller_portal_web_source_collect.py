"""Canonical dated collector. Default is a private, read-only candidate."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.seller_portal_automation_guard import seller_portal_automation_lock
from packages.adapters.seller_portal_web_source_collector import CollectorError, collect_web_source
from packages.adapters.web_source_current_sync import _load_env_file


def search_candidate_nm_ids(day: str, *, canonical_env: dict, runtime_dir: Path) -> list[int]:
    """Read known Seller SKU identities; the live global count remains authority."""
    parsed=date.fromisoformat(day)
    if parsed.isoformat()!=day:raise ValueError('canonical_date_required')
    known=set()
    manifest=json.loads((runtime_dir/'storage_generation_manifest.json').read_text())
    if manifest.get('canonical_source')!='split':raise ValueError('storage_authority_invalid')
    db=(runtime_dir/manifest['operational']['relative_path']).resolve()
    if not db.is_relative_to(runtime_dir.resolve()/'generations'):
        raise ValueError('storage_target_invalid')
    conn=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=30)
    try:
        conn.execute('PRAGMA query_only=ON')
        bundle=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
        known.update(int(r[0]) for r in conn.execute('SELECT nm_id FROM registry_upload_config_v2 WHERE bundle_version=?',(bundle,)))
        ready=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? ORDER BY as_of_date DESC LIMIT 1',(bundle,)).fetchone()
        if ready:
            for sheet in json.loads(ready[0]).get('sheets',[]):
                if sheet.get('sheet_name')=='DATA_VITRINA':
                    for row in sheet.get('rows',[]):
                        key=row[1] if len(row)>1 else ''
                        if isinstance(key,str) and key.startswith('SKU:'):
                            known.add(int(key.split('|',1)[0][4:]))
    finally:conn.close()
    import psycopg2
    connection=psycopg2.connect(**{name:canonical_env['WEB_SOURCE_SRC_'+key] for name,key in
        [('host','PGHOST'),('port','PGPORT'),('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD')]},connect_timeout=5)
    try:
        connection.set_session(readonly=True,isolation_level='REPEATABLE READ')
        start=parsed-timedelta(days=30);end=parsed+timedelta(days=7)
        with connection.cursor() as cur:
            cur.execute('SELECT DISTINCT nm_id FROM public.sales_funnel_daily_raw WHERE snapshot_date BETWEEN %s AND %s '
                'UNION SELECT DISTINCT nm_id FROM public.search_analytics_raw WHERE date_to BETWEEN %s AND %s',
                (start,end,start,end))
            known.update(int(r[0]) for r in cur.fetchall())
    finally:connection.rollback();connection.close()
    if not known or len(known)>400 or any(nm<=0 for nm in known):
        raise ValueError('search_candidate_universe_invalid')
    return sorted(known)


def write_source(candidate: dict, *, env: dict) -> None:
    """Replace one dated source in one transaction, preserving observation time."""
    day = candidate['snapshot_date']
    items = candidate['items']
    if not items or candidate.get('completeness') != 'complete' or candidate['request_period'] != {'start':day,'end':day}:
        raise ValueError('unqualified_source_candidate')
    signals=('view_count','open_card_count') if candidate['source_key']=='seller_funnel_snapshot' else ('views_current','ctr_current','orders_current')
    if not any((r.get(k) or 0)>0 for r in items for k in signals):
        raise ValueError('source_zero_signal_not_accepted')
    import psycopg2
    from psycopg2.extras import Json, execute_batch
    conn = psycopg2.connect(host=env.get('WEB_DB_HOST','127.0.0.1'), port=env.get('WEB_DB_PORT','5432'),
        dbname=env.get('WEB_DB_NAME','wb_web_analytics'), user=env.get('WEB_DB_USER','wb_ai'),
        password=env.get('WEB_DB_PASSWORD'), connect_timeout=5)
    try:
        with conn:
            with conn.cursor() as cur:
                if candidate['source_key'] == 'seller_funnel_snapshot':
                    cur.execute('DELETE FROM public.sales_funnel_daily_raw WHERE snapshot_date=%s', (day,))
                    execute_batch(cur, 'INSERT INTO public.sales_funnel_daily_raw (snapshot_date,nm_id,name,vendor_code,view_count,open_card_count,ctr,fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                        [(day, r['nm_id'], r['name'], r['vendor_code'], r['view_count'], r['open_card_count'], r['ctr'], candidate['source_fetched_at']) for r in items])
                elif candidate['source_key'] == 'web_source_snapshot':
                    cur.execute('DELETE FROM public.search_analytics_raw WHERE date_from=%s AND date_to=%s', (day,day))
                    execute_batch(cur, 'INSERT INTO public.search_analytics_raw (date_from,date_to,nm_id,views_current,ctr_current,orders_current,position_avg,raw_json,fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        [(day,day,r['nm_id'],r['views_current'],r['ctr_current'],r['orders_current'],r['position_avg'],Json(candidate['raw_report']),candidate['source_fetched_at']) for r in items])
                else:
                    raise ValueError('unsupported_source_key')
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-key', choices=['seller_funnel_snapshot','web_source_snapshot'], required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--bot-dir', default='/opt/wb-web-bot')
    parser.add_argument('--canonical-env', default='/opt/wb-ai/.env')
    parser.add_argument('--runtime-dir', default='/opt/wb-core-runtime/state')
    parser.add_argument('--write-source', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    bot = Path(args.bot_dir)
    env = {**os.environ, **_load_env_file(bot/'.env')}
    canonical_env=_load_env_file(Path(args.canonical_env))
    canonical_supplier=canonical_env.get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','')
    if not canonical_supplier:raise ValueError('canonical_supplier_not_configured')
    with seller_portal_automation_lock(owner='web_source_collector', purpose='dated_report', run_id=str(uuid4()), expected_max_seconds=600):
        known=search_candidate_nm_ids(args.date,canonical_env=canonical_env,runtime_dir=Path(args.runtime_dir)) if args.source_key=='web_source_snapshot' else None
        candidate = collect_web_source(source_key=args.source_key, snapshot_date=args.date,
            storage_state_path=str(bot/'storage_state.json'), canonical_supplier_id=canonical_supplier,
            search_candidate_nm_ids=known)
        if args.write_source:
            write_source(candidate, env=env)
        if args.output:
            with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600),'w') as f:
                json.dump(candidate,f,ensure_ascii=False,sort_keys=True)
        print(json.dumps({k:v for k,v in candidate.items() if k not in {'items','raw_report','detail_pages','search_batches'}} | {'row_count':len(candidate['items']), 'source_written':args.write_source}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, CollectorError) else type(exc).__name__
        print('seller_source_collector_failed:' + reason, file=sys.stderr)
        raise SystemExit(1) from None
