"""Explicit, durable batch orchestration over the guarded one-target worker."""
from __future__ import annotations

import json

from packages.application.search_cluster_cleaner import BATCH_TERMINAL_STATES, batch_child_id
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.contracts.search_cluster_cleaner import CleanerError, Principal


def _drift_only_scan(c, cleaner, job:dict, frozen:dict) -> dict|None:
    """Prove that this child only observed external drift and never wrote WB.

    A complete scan can still be partial because a previously excluded phrase
    disappeared from WB minus. Its target hold remains in place. This proof is
    for advancing an unrelated batch pair, never for clearing that hold.
    """
    if (job.get('state')!='failed' or job.get('stage')!='finished' or job.get('can_recheck')
            or job.get('write_run_id') or job.get('advert_id')!=frozen['advert_id']
            or job.get('nm_id')!=frozen['nm_id']):return None
    run_id=job.get('scan_run_id')
    if not run_id:return None
    target=f"{frozen['advert_id']}:{frozen['nm_id']}"
    run=c.execute('SELECT * FROM cleaner_runs WHERE account=? AND run_id=?',(cleaner.key,run_id)).fetchone()
    if (not run or run['kind']!='scan' or run['trigger']!='manual_exact' or run['state']!='partial'
            or run['phase']!='finished' or not run['scan_finished_at'] or run['reason']
            or json.loads(run['targets'])!=[dict(target=target,advert_id=frozen['advert_id'],nm_id=frozen['nm_id'])]):return None
    rows=c.execute('SELECT target,state,complete,reason FROM cleaner_run_targets WHERE run_id=?',(run_id,)).fetchall()
    if (len(rows)!=1 or rows[0]['target']!=target or rows[0]['state']!='partial'
            or rows[0]['complete']!=1 or rows[0]['reason']!='external_state_drift'):return None
    summary=json.loads(run['summary'])
    if (summary.get('pairs')!=1 or not summary.get('dry_run')
            or any(summary.get(field)!=0 for field in ('profile_required','excluded_not_executed',
                'unresolved_operations','requires_review_operations','rejected_not_executed'))):return None
    hold=c.execute('SELECT reason FROM cleaner_target_holds WHERE account=? AND target=?',(cleaner.key,target)).fetchone()
    if not hold or hold['reason']!='external_state_drift':return None
    if (c.execute('SELECT 1 FROM cleaner_write_operations WHERE account=? AND run_id=?',(cleaner.key,run_id)).fetchone()
            or c.execute("SELECT 1 FROM cleaner_events WHERE account=? AND kind='manual_apply_prepared' AND json_extract(facts,'$.scan_run_id')=?",(cleaner.key,run_id)).fetchone()):return None
    events=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='external_state_drift' ORDER BY sequence",
                     (cleaner.key,run_id)).fetchall()
    if not events:return None
    phrases=[];seen=set()
    for event in events:
        facts=json.loads(event['facts']);query_identity=facts.get('query_hash')
        if facts.get('target')!=target or not isinstance(query_identity,str):return None
        if query_identity in seen:continue  # Both drift detectors may report one exact phrase.
        seen.add(query_identity)
        observed=c.execute('''SELECT o.query,o.state,o.query_hash,d.verdict,d.rule_id,d.reason,d.source
            FROM cleaner_observations o LEFT JOIN cleaner_auto_decisions d ON d.decision_id=o.decision_id
            WHERE o.account=? AND o.target=? AND o.query_hash=?''',(cleaner.key,target,query_identity)).fetchone()
        if not observed or observed['state']!='external_state_drift':return None
        phrases.append(dict(observed))
    return dict(target=target,queries=phrases,scan_run_id=run_id)


def _drift_item_update(job:dict,proof:dict) -> dict:
    return dict(state='skipped',stage='finished',job_id=job['job_id'],
                error_code='external_state_drift',
                error='Список исключений WB изменился; эта пара оставлена на разборе без новой отправки',
                review_required=True,drift_queries=proof['queries'])


def _terminal_drift_resume_plan(c,cleaner,batch_id:str,actor:str,actor_authority:str|None=None) -> dict|None:
    """Read-only CAS precondition for resuming one exact terminal batch tail."""
    request=c.execute("SELECT actor FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",
                      (cleaner.key,batch_id)).fetchone()
    if not request or request['actor']!=actor:return None
    rows=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence",
                   (cleaner.key,batch_id)).fetchall()
    if not rows:return None
    original=json.loads(rows[0]['facts']);latest=json.loads(rows[-1]['facts'])
    items=original.get('items');updates=latest.get('item_updates')
    if (not isinstance(items,list) or not items or not isinstance(updates,dict)
            or latest.get('state') not in {'partial','failed'} or latest.get('stage')!='finished'
            or latest.get('current_index')!=len(items)):return None
    newest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' ORDER BY sequence DESC LIMIT 1",
                     (cleaner.key,)).fetchone()
    settings=cleaner._settings(c)
    if (not newest or json.loads(newest['facts']).get('batch_id')!=batch_id
            or original.get('actor')!=actor or original.get('account_key')!=cleaner.key
            or (actor_authority is not None and original.get('actor_authority','configured_owner')!=actor_authority)
            or original.get('generation')!=settings['generation'] or settings['enabled'] or not settings['baseline_ready']
            or c.execute("SELECT 1 FROM cleaner_runs WHERE account=? AND state IN('queued','accepted','running')",(cleaner.key,)).fetchone()):return None
    failed=[index for index in range(len(items)) if updates.get(str(index),{}).get('state')=='failed']
    if len(failed)!=1:return None
    index=failed[0]
    if (any(updates.get(str(i),{}).get('state') not in {'complete','no_change','skipped'}
            or updates[str(i)].get('error_code')=='not_started_after_failure' for i in range(index))
            or any(updates.get(str(i),{})!={'state':'skipped','stage':'not_started',
                     'error_code':'not_started_after_failure','error':'Предыдущая пара не завершилась'}
                   for i in range(index+1,len(items)))):return None
    latest_job=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_requested' ORDER BY sequence DESC LIMIT 1",
                         (cleaner.key,)).fetchone()
    if not latest_job or json.loads(latest_job['facts']).get('job_id')!=batch_child_id(batch_id,index):return None
    for earlier in range(index):
        earlier_id=batch_child_id(batch_id,earlier)
        prior=c.execute("SELECT outcome FROM cleaner_requests WHERE account=? AND actor=? AND request_id=? AND route='manual-clean'",
                        (cleaner.key,actor,earlier_id)).fetchone()
        update=updates[str(earlier)]
        if update['state']=='skipped':
            if prior:return None
            continue
        if not prior:return None
        outcome=json.loads(prior['outcome'])
        if (outcome.get('batch_id')!=batch_id or outcome.get('batch_index')!=earlier
                or outcome.get('advert_id')!=items[earlier]['advert_id']
                or outcome.get('nm_id')!=items[earlier]['nm_id']):return None
        prior_event=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",
                              (cleaner.key,earlier_id)).fetchone()
        if not prior_event:return None
        prior_facts=json.loads(prior_event['facts'])
        if prior_facts.get('state')!=update['state'] or prior_facts.get('can_recheck'):return None
    job_id=batch_child_id(batch_id,index)
    child_request=c.execute("SELECT outcome FROM cleaner_requests WHERE account=? AND actor=? AND request_id=? AND route='manual-clean'",
                            (cleaner.key,actor,job_id)).fetchone()
    if not child_request:return None
    outcome=json.loads(child_request['outcome'])
    if (outcome.get('batch_id')!=batch_id or outcome.get('batch_index')!=index
            or outcome.get('advert_id')!=items[index]['advert_id'] or outcome.get('nm_id')!=items[index]['nm_id']):return None
    child_event=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%' AND json_extract(facts,'$.job_id')=? ORDER BY sequence DESC LIMIT 1",
                          (cleaner.key,job_id)).fetchone()
    if not child_event:return None
    job=dict(outcome,**json.loads(child_event['facts']))
    if job.get('job_id')!=job_id or job.get('batch_id')!=batch_id or job.get('batch_index')!=index:return None
    proof=_drift_only_scan(c,cleaner,job,items[index])
    if not proof:return None
    for later in range(index+1,len(items)):
        child_id=batch_child_id(batch_id,later)
        if c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-clean'",
                     (cleaner.key,child_id)).fetchone():return None
    return dict(index=index,job=job,proof=proof,previous=latest)


def _child(cleaner, owner, batch_id: str, index: int, frozen:dict):
    job_id=batch_child_id(batch_id,index)
    with cleaner.store.read() as c:
        request=c.execute("SELECT outcome FROM cleaner_requests WHERE account=? AND actor=? AND request_id=? AND route='manual-clean'",
                         (cleaner.key,owner.username.strip().casefold(),job_id)).fetchone()
    if not request:return None
    outcome=json.loads(request['outcome'])
    if (outcome.get('batch_id')!=batch_id or outcome.get('batch_index')!=index
            or outcome.get('advert_id')!=frozen['advert_id'] or outcome.get('nm_id')!=frozen['nm_id']):
        raise CleanerError('batch_child_collision','Сохранённое задание не принадлежит этой группе',409)
    job=cleaner.manual_job(job_id,owner)
    if (job.get('batch_id')!=batch_id or job.get('batch_index')!=index
            or job.get('advert_id')!=frozen['advert_id'] or job.get('nm_id')!=frozen['nm_id']):
        raise CleanerError('batch_child_collision','Журнал задания не соответствует группе',409)
    return job


def batch_status(cleaner, batch_id: str, principal: Principal) -> dict:
    saved=cleaner.manual_batch_snapshot(batch_id,principal)
    items=[]
    for index,frozen in enumerate(saved['items']):
        update=saved['item_updates'].get(str(index),{})
        try:job=_child(cleaner,principal,batch_id,index,frozen)
        except CleanerError as exc:
            if exc.code!='batch_child_collision':raise
            job=None
            update=dict(update,state='failed',stage='finished',error_code=exc.code,error=str(exc))
        proof=None
        if job and job['state']=='failed':
            with cleaner.store.read() as c:proof=_drift_only_scan(c,cleaner,job,frozen)
        state=update.get('state') or (job['state'] if job else 'queued')
        row=dict(index=index,**frozen,selected_status=frozen['status'],state=state,
                 stage=update.get('stage') or (job.get('stage') if job else 'queued'),
                 job_id=job.get('job_id') if job else None,
                 write_run_id=job.get('write_run_id') if job else None,
                 error=(_drift_item_update(job,proof)['error'] if proof else update.get('error') or (job.get('error') if job else None)),
                 error_code=('external_state_drift' if proof else update.get('error_code') or (job.get('error_code') if job else None)),
                 result=update.get('result') or (job.get('result') if job else None),
                 can_recheck=bool(job and job.get('can_recheck')),
                 review_required=bool(update.get('review_required') or proof),
                 drift_queries=update.get('drift_queries') or (proof['queries'] if proof else []),
                 new_checked=None,confirmed_excluded=None,allowed=None,review_count=None,
                 pending_count=None,not_sent_count=None,delivery_state='unknown',already_excluded=None)
        if job:
            with cleaner.store.read() as c:
                scan=c.execute("SELECT state,summary FROM cleaner_runs WHERE account=? AND run_id=?",(cleaner.key,job['scan_run_id'])).fetchone()
            if scan and (scan['state']=='complete' or proof):
                counts=json.loads(scan['summary'])
                row['new_checked']=counts.get('new_checked')
                row['allowed']=counts.get('allow')
                row['review_count']=counts.get('review')
                decisions=job.get('scan_decisions',[])
                row['already_excluded']=sum(1 for d in decisions if d['state'] in {'already_excluded','observed_excluded'})
            if job.get('write_run_id'):
                run=cleaner.run_detail(job['write_run_id'],principal)
                phrases=run['phrases']
                active=[op for op in run['write_operations'] if op['state']!='cancelled_before_send']
                written_queries={p['query'] for p in phrases}
                if row['already_excluded'] is not None:
                    row['already_excluded']=sum(1 for d in job.get('scan_decisions',[])
                                                if d['state'] in {'already_excluded','observed_excluded'} and d['query'] not in written_queries)
                row['confirmed_excluded']=sum(1 for p in phrases if p['state']=='confirmed' and p['confirmed_at'])
                unresolved=sum(1 for p in phrases if p['state'] not in {'confirmed','rejected'} or
                               (p['state']=='confirmed' and not p['confirmed_at']))
                if any(op['state'] in {'dispatching','submitted','unresolved','validation_rejected','rate_limited',
                                      'unauthorized','forbidden','transport_ambiguous','http_error','requires_review'} for op in active):
                    row['delivery_state']='wb_pending';row['pending_count']=unresolved;row['not_sent_count']=0
                elif active and all(op['state']=='confirmed' for op in active) and not unresolved:
                    row['delivery_state']='confirmed';row['pending_count']=0;row['not_sent_count']=0
                elif active and all(op['state']=='prepared' for op in active):
                    # SQLite has no committed dispatch right. An external seal
                    # still needs recovery proof before claiming no WB send.
                    row['delivery_state']='local_prepared';row['pending_count']=0;row['not_sent_count']=unresolved
                elif not active and unresolved and any(op['state']=='cancelled_before_send' for op in run['write_operations']):
                    row['delivery_state']='not_sent';row['pending_count']=0;row['not_sent_count']=unresolved
            elif job['state'] in {'no_change','complete'}:
                row['confirmed_excluded']=0
                row['pending_count']=0
                row['not_sent_count']=0
        items.append(row)
    done={'complete','no_change','partial','failed','skipped'}
    not_started=lambda item:item['stage']=='not_started' or item['error_code']=='not_started_after_failure'
    counts={name:sum(item['state']==name and not not_started(item) and not item['review_required'] for item in items)
            for name in done}
    not_started_count=sum(not_started(item) for item in items)
    try:
        principal.require_owner(cleaner.owner_username)
        with cleaner.store.read() as c:resume=_terminal_drift_resume_plan(c,cleaner,batch_id,principal.username.strip().casefold(),
            'bootstrap_operator' if principal.site_owner else 'configured_owner')
    except CleanerError:resume=None
    current_index=saved['current_index']
    current=items[current_index] if type(current_index) is int and 0<=current_index<len(items) and saved['state'] not in BATCH_TERMINAL_STATES else None
    return dict(batch_id=batch_id,state=saved['state'],stage=saved['stage'],
                selected_categories=saved['selected_categories'],selected_count=len(items),
                done_count=sum(item['state'] in done and not item['can_recheck'] and not not_started(item) for item in items),confirmed_count=counts['complete'],
                completed_count=counts['complete']+counts['no_change'],
                no_change_count=counts['no_change'],partial_count=counts['partial'],failed_count=counts['failed'],
                skipped_count=counts['skipped'],not_started_count=not_started_count,
                held_count=sum(item['review_required'] for item in items),
                can_resume=bool(resume),resume_index=resume['index'] if resume else None,current_index=current_index,
                current_target={k:current[k] for k in ('advert_id','nm_id','campaign_name','product_title')} if current else None,
                created_at=saved['created_at'],updated_at=saved['updated_at'],
                error=saved['error'],error_code=saved['error_code'],items=items)


def batch_item_detail(cleaner,batch_id:str,index:int,principal:Principal) -> dict:
    status=batch_status(cleaner,batch_id,principal)
    if index<0 or index>=len(status['items']):raise CleanerError('not_found','Пара не найдена',404)
    item=status['items'][index]
    try:job=_child(cleaner,principal,batch_id,index,status['items'][index])
    except CleanerError as exc:
        if exc.code!='batch_child_collision':raise
        job=None
    run=cleaner.run_detail(job['write_run_id'],principal) if job and job.get('write_run_id') else None
    return dict(item=item,job=job,run=run)


class BatchCleanerCoordinator:
    def __init__(self,cleaner,*,generation:str,source_factory=None,fixture_admission=None,bootstrap_owner_username:str=''):
        self.cleaner=cleaner
        self.generation=generation
        self.source_factory=source_factory
        self.fixture_admission=fixture_admission
        self.bootstrap_owner_username=bootstrap_owner_username.strip().casefold()
        self.owner=Principal(cleaner.owner_username,True,True,True)

    def _actor_for_batch(self,batch_id:str) -> Principal:
        with self.cleaner.store.read() as c:
            request=c.execute("SELECT actor FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",
                              (self.cleaner.key,batch_id)).fetchone()
            initial=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence LIMIT 1",
                              (self.cleaner.key,batch_id)).fetchone()
        if not request or not initial:raise CleanerError('batch_authority_missing','Владелец группы не подтверждён',409)
        facts=json.loads(initial['facts']);actor=request['actor']
        if (facts.get('actor')!=actor or facts.get('account_key',self.cleaner.key)!=self.cleaner.key
                or facts.get('generation',self.generation)!=self.generation):
            raise CleanerError('batch_authority_mismatch','Владелец группы изменился',409)
        authority=facts.get('actor_authority','configured_owner')
        if authority=='bootstrap_operator':
            if not self.bootstrap_owner_username or actor!=self.bootstrap_owner_username:
                raise CleanerError('batch_authority_mismatch','Владелец группы изменился',409)
            return Principal(actor,True,True,True,site_owner=True)
        if authority!='configured_owner' or actor!=self.cleaner.owner_username.strip().casefold():
            raise CleanerError('batch_authority_mismatch','Владелец группы изменился',409)
        return Principal(actor,True,True,True)

    def pending_batches(self) -> list[str]:
        with self.cleaner.store.read() as c:
            rows=c.execute("SELECT json_extract(facts,'$.batch_id') AS batch_id FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' ORDER BY sequence",(self.cleaner.key,)).fetchall()
            pending=[]
            for row in rows:
                latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence DESC LIMIT 1",(self.cleaner.key,row['batch_id'])).fetchone()
                if json.loads(latest['facts']).get('state','queued') not in BATCH_TERMINAL_STATES:pending.append(row['batch_id'])
            return pending

    def _exact_eligibility(self,item:dict) -> dict:
        if self.source_factory is None:
            from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
            source=CleanerWbSource.from_env(self.cleaner.account)
        else:source=self.source_factory()
        targets,missing=source._adverts([item['advert_id']],source.monotonic()+120,strict=False)
        if missing and missing!=['adverts_missing:'+str(item['advert_id'])]:
            raise CleanerError('campaign_catalog_incomplete','WB вернул неполную кампанию',409)
        rows=eligibility_rows(self.cleaner,self.generation,targets,fixture_admission=self.fixture_admission)
        row=next((r for r in rows if r['advert_id']==item['advert_id'] and r['nm_id']==item['nm_id']),None)
        if row is None:raise CleanerError('campaign_sku_missing','Пара больше не найдена в кампании WB',409)
        return row

    def _stop(self,batch:dict,index:int,*,code:str,message:str,item_state:str='failed') -> None:
        updates=dict(batch['item_updates'])
        updates[str(index)]=dict(state=item_state,stage='finished',error_code=code,error=message)
        for later in range(index+1,len(batch['items'])):
            updates[str(later)]=dict(state='skipped',stage='not_started',error_code='not_started_after_failure',error='Предыдущая пара не завершилась')
        earlier=any(v.get('state') in {'complete','no_change'} for v in updates.values())
        self.cleaner.record_manual_batch(batch['batch_id'],state='partial' if earlier else 'failed',stage='finished',
                                         current_index=len(batch['items']),item_updates=updates,error_code=code,error=message)

    def tick(self) -> dict|None:
        ids=self.pending_batches()
        if not ids:return None
        self.owner=self._actor_for_batch(ids[0])
        batch=self.cleaner.manual_batch_snapshot(ids[0],self.owner)
        index=batch['current_index'] if type(batch['current_index']) is int else 0
        if index>=len(batch['items']):
            states=[batch['item_updates'].get(str(i),{}).get('state') for i in range(len(batch['items']))]
            state='complete' if all(s in {'complete','no_change'} for s in states) else 'partial'
            self.cleaner.record_manual_batch(batch['batch_id'],state=state,stage='finished',current_index=index,error=None,error_code=None)
            return batch_status(self.cleaner,batch['batch_id'],self.owner)
        item=batch['items'][index]
        try:child=_child(self.cleaner,self.owner,batch['batch_id'],index,item)
        except CleanerError as exc:
            self._stop(batch,index,code=exc.code,message=str(exc))
            return batch_status(self.cleaner,batch['batch_id'],self.owner)
        if child:
            if child['state']=='partial' and child.get('can_recheck'):
                if batch['state']!='attention_required':
                    self.cleaner.record_manual_batch(batch['batch_id'],state='attention_required',stage='readback',current_index=index,
                                                     error_code='readback_unresolved',error='Уточните состояние текущей пары и продолжите группу')
                return batch_status(self.cleaner,batch['batch_id'],self.owner)
            if child['state'] in {'complete','no_change'}:
                self.cleaner.record_manual_batch(batch['batch_id'],state='running',stage='next_target',current_index=index+1,error=None,error_code=None,
                                                 item_update=(index,dict(state=child['state'],stage='finished',job_id=child['job_id'])))
                return batch_status(self.cleaner,batch['batch_id'],self.owner)
            if child['state'] in {'failed','partial'}:
                with self.cleaner.store.read() as c:drift=_drift_only_scan(c,self.cleaner,child,item)
                if drift:
                    self.cleaner.record_manual_batch(batch['batch_id'],state='running',stage='next_target',current_index=index+1,
                        error=None,error_code=None,item_update=(index,_drift_item_update(child,drift)))
                    return batch_status(self.cleaner,batch['batch_id'],self.owner)
                self._stop(batch,index,code=child.get('error_code') or 'child_failed',message=child.get('error') or 'Проверка пары не завершилась')
                return batch_status(self.cleaner,batch['batch_id'],self.owner)
            if batch['state']!='running' or batch['stage']!=child['stage']:
                self.cleaner.record_manual_batch(batch['batch_id'],state='running',stage=child['stage'],current_index=index,error=None,error_code=None)
            return batch_status(self.cleaner,batch['batch_id'],self.owner)
        try:
            row=self._exact_eligibility(item)
        except CleanerError as exc:
            self._stop(batch,index,code=exc.code,message=str(exc))
            return batch_status(self.cleaner,batch['batch_id'],self.owner)
        except Exception:
            self._stop(batch,index,code='campaign_catalog_unavailable',message='Не удалось проверить текущую кампанию WB')
            return batch_status(self.cleaner,batch['batch_id'],self.owner)
        if not row['eligible'] or row['status'] not in batch['selected_categories']:
            reason=row['reason'] or 'selected_status_changed'
            self.cleaner.record_manual_batch(batch['batch_id'],state='running',stage='next_target',current_index=index+1,error=None,error_code=None,
                                             item_update=(index,dict(state='skipped',stage='finished',error_code=reason,
                                                                     error='Пара больше не соответствует выбранным статусам или допуску')))
            return batch_status(self.cleaner,batch['batch_id'],self.owner)
        self.cleaner.record_manual_batch(batch['batch_id'],state='running',stage='dispatching',current_index=index,error=None,error_code=None)
        try:
            self.cleaner.start_manual_clean(dict(request_id=batch_child_id(batch['batch_id'],index),advert_id=item['advert_id'],nm_id=item['nm_id']),
                                            self.owner,batch_id=batch['batch_id'],batch_index=index)
        except CleanerError as exc:
            self._stop(batch,index,code=exc.code,message=str(exc))
        return batch_status(self.cleaner,batch['batch_id'],self.owner)
