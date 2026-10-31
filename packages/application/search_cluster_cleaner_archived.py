"""Exact archive retirement evidence; never a retry of a rejected WB write."""
import json
from packages.adapters.search_cluster_cleaner_wb import archived_minus_rejection
from packages.contracts.search_cluster_cleaner import digest, query_hash

POLICY='wb_archived_retirement_v1'


def rejected_archived_queries(receipt):
    if not archived_minus_rejection(receipt.get('status'),receipt.get('body_excerpt'),
            truncated=receipt.get('body_truncated',True),capture_error=receipt.get('body_capture_error','')):
        return ()
    value=json.loads(receipt['body_excerpt'])['detail'][len('cannot exclude archived presets: '):]
    if not value.startswith("'") or not value.endswith("'"):return ()
    queries=value[1:-1].split("', '")
    # Ambiguous quoting is not permission to remove any phrase.
    if not queries or any(not q or "'" in q for q in queries) or len(set(queries))!=len(queries):return ()
    return tuple(queries)


def reconcile_rejected_archive_hold(app,c,run_id,snapshot):
    from packages.application.search_cluster_cleaner import timestamp
    key=snapshot.target.key
    hold=c.execute('SELECT reason FROM cleaner_target_holds WHERE account=? AND target=?',(app.key,key)).fetchone()
    if not hold or hold['reason']!='known_validation_rejected':return False
    op=c.execute('SELECT * FROM cleaner_write_operations WHERE account=? AND target=? ORDER BY rowid DESC LIMIT 1',(app.key,key)).fetchone()
    if not op or op['state']!='rejected' or op['dispatch_count']!=1 or op['worker_generation']!=app._settings(c)['generation']:return False
    if c.execute("SELECT 1 FROM cleaner_write_operations WHERE account=? AND target=? AND state NOT IN ('confirmed','rejected','cancelled_before_send')",(app.key,key)).fetchone():return False
    evidence=json.loads(op['evidence']);reported=rejected_archived_queries(evidence.get('receipt',{}))
    before=json.loads(op['before_json']);expected=json.loads(op['expected_json']);rb=evidence.get('readback',{})
    if (not reported or not set(reported)<=set(expected) or not set(reported)<=set(snapshot.archived)
            or not set(reported)<=set(snapshot.minus) or sorted(snapshot.minus)!=before
            or rb.get('status')!='read' or rb.get('minus')!=before or rb.get('missing_old')!=[] or rb.get('extra')!=sorted(set(before)-set(expected))):return False
    times=[snapshot.observed_at]+[snapshot.source_times.get(k) for k in ('list','statistics','minus')]
    if any(not t or not 0<=(timestamp(app.clock())-timestamp(t)).total_seconds()<=30 for t in times):return False
    if not c.execute("SELECT 1 FROM cleaner_readback_jobs WHERE account=? AND operation_id=? AND state='done'",(app.key,op['operation_id'])).fetchone():return False
    items=c.execute('SELECT query,query_hash,decision_id,state,confirmed_at FROM cleaner_write_items WHERE operation_id=?',(op['operation_id'],)).fetchall()
    frozen=json.loads(op['versions'])['items']
    if (not items or any(r['state']!='rejected' or r['confirmed_at'] or query_hash(r['query'])!=r['query_hash'] for r in items)
            or {(r['query_hash'],r['decision_id']) for r in items}!={(r['query_hash'],r['decision_id']) for r in frozen}):return False
    if not c.execute("SELECT 1 FROM cleaner_events WHERE account=? AND operation_id=? AND run_id=? AND kind='write_rejected' AND json_extract(facts,'$.target')=? AND json_extract(facts,'$.reason')='known_validation_rejected'",(app.key,op['operation_id'],op['run_id'],key)).fetchone():return False
    c.execute("DELETE FROM cleaner_target_holds WHERE account=? AND target=? AND reason='known_validation_rejected'",(app.key,key))
    app._event(c,'archived_rejection_reconciled',dict(target=key,rejected_operation_id=op['operation_id'],
        policy=POLICY,reported_queries=list(reported),archived=list(snapshot.archived),
        before_digest=digest(before),source_times=dict(snapshot.source_times),observed_at=snapshot.observed_at),run_id=run_id)
    return True
