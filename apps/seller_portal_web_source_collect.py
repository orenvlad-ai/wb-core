"""Canonical dated collector. Default is a private, read-only candidate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.seller_portal_automation_guard import seller_portal_automation_lock
from packages.adapters.seller_portal_web_source_collector import CollectorError, collect_web_source
from packages.adapters.web_source_current_sync import _load_env_file


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
    parser.add_argument('--write-source', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    bot = Path(args.bot_dir)
    env = {**os.environ, **_load_env_file(bot/'.env')}
    canonical_supplier=_load_env_file(Path(args.canonical_env)).get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','')
    if not canonical_supplier:raise ValueError('canonical_supplier_not_configured')
    with seller_portal_automation_lock(owner='web_source_collector', purpose='dated_report', run_id=str(uuid4()), expected_max_seconds=600):
        candidate = collect_web_source(source_key=args.source_key, snapshot_date=args.date,
            storage_state_path=str(bot/'storage_state.json'), canonical_supplier_id=canonical_supplier)
        if args.write_source:
            write_source(candidate, env=env)
        if args.output:
            with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600),'w') as f:
                json.dump(candidate,f,ensure_ascii=False,sort_keys=True)
        print(json.dumps({k:v for k,v in candidate.items() if k not in {'items','raw_report','detail_pages'}} | {'row_count':len(candidate['items']), 'source_written':args.write_source}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, CollectorError) else type(exc).__name__
        print('seller_source_collector_failed:' + reason, file=sys.stderr)
        raise SystemExit(1) from None
