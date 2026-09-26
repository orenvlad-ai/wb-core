"""Explicit, durable batch orchestration over the guarded one-target worker."""
from __future__ import annotations

import json

from packages.application.search_cluster_cleaner import BATCH_TERMINAL_STATES, batch_child_id
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.contracts.search_cluster_cleaner import CleanerError, Principal


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
        state=update.get('state') or (job['state'] if job else 'queued')
        row=dict(index=index,**frozen,selected_status=frozen['status'],state=state,
                 stage=update.get('stage') or (job.get('stage') if job else 'queued'),
                 job_id=job.get('job_id') if job else None,
                 write_run_id=job.get('write_run_id') if job else None,
                 error=update.get('error') or (job.get('error') if job else None),
                 error_code=update.get('error_code') or (job.get('error_code') if job else None),
                 result=update.get('result') or (job.get('result') if job else None),
                 can_recheck=bool(job and job.get('can_recheck')),
                 new_checked=None,confirmed_excluded=None,allowed=None,review_count=None,
                 pending_count=None,not_sent_count=None,delivery_state='unknown',already_excluded=None)
        if job:
            with cleaner.store.read() as c:
                scan=c.execute("SELECT state,summary FROM cleaner_runs WHERE account=? AND run_id=?",(cleaner.key,job['scan_run_id'])).fetchone()
            if scan and scan['state']=='complete':
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
    counts={name:sum(item['state']==name for item in items) for name in done}
    current_index=saved['current_index']
    current=items[current_index] if type(current_index) is int and 0<=current_index<len(items) and saved['state'] not in BATCH_TERMINAL_STATES else None
    return dict(batch_id=batch_id,state=saved['state'],stage=saved['stage'],
                selected_categories=saved['selected_categories'],selected_count=len(items),
                done_count=sum(item['state'] in done and not item['can_recheck'] for item in items),confirmed_count=counts['complete'],
                no_change_count=counts['no_change'],partial_count=counts['partial'],failed_count=counts['failed'],
                skipped_count=counts['skipped'],current_index=current_index,
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
