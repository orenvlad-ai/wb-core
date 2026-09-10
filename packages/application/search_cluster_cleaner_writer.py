"""Full-set one-submit cleaner transaction and independent, durable readback.

The single operational commit is the dispatch boundary. Network and rate waits
are outside SQLite. A crash after that commit grants successors readback only.
"""
from __future__ import annotations
from dataclasses import asdict
import json
from packages.application.search_cluster_cleaner import new_id, timestamp, plus_seconds
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.business_data_write_barrier import barrier_status
from packages.contracts.search_cluster_cleaner import CleanerError, Target, canonical, digest, query_hash
from packages.adapters.search_cluster_cleaner_wb import WriteResponse


class CleanerWriter:
    def __init__(self,cleaner,source,session,*,generation,preflight_max_age=30,hook=None,registry=None):
        self.app,self.source,self.session,self.generation=cleaner,source,session,generation
        if source.account!=cleaner.account:raise CleanerError('account_mismatch','Источник другого аккаунта')
        self.registry=registry or ChangeRegistryRepository(cleaner.store.registry.runtime_dir)
        self.preflight_max_age=preflight_max_age;self.hook=hook or (lambda stage,op:None)

    def _guard(self,c,run_id,token):
        run=self.app._lease(c,run_id,token,self.generation,require_enabled=True)
        s=self.app._settings(c)
        if s['restore_hold'] or not s['baseline_ready']:raise CleanerError('restore_hold','Исходная база не допущена',409)
        if s['rules_version']!=self.app.rules_version:raise CleanerError('rules_changed','Версия правил изменилась',409)
        if json.loads(run['captured_versions']).get('rules_hash')!=self.app.rules_digest:
            raise CleanerError('rules_changed','Исполняемые правила изменились',409)
        if barrier_status(self.app.store.registry.runtime_dir)['active']:raise CleanerError('maintenance','Обслуживание блокирует новые записи',423)
        self.session.check(c,self.app.account,self.generation)
        return s,run

    def _current(self,c,row):
        app=self.app
        obs=c.execute('SELECT * FROM cleaner_observations WHERE account=? AND target=? AND query_hash=?',(app.key,row['target'],row['query_hash'])).fetchone()
        if not obs or obs['query']!=row['query'] or obs['decision_id']!=row['decision_id'] or obs['state']!='pending_exclude':
            raise CleanerError('decision_changed','Кандидат уже изменился',409)
        d=c.execute('SELECT * FROM cleaner_auto_decisions WHERE decision_id=?',(obs['decision_id'],)).fetchone()
        p=app._profile(c,obs['nm_id'])
        if (not d or d['verdict']!='exclude' or not p or p.version!=d['profile_version'] or p.semantic_fingerprint!=d['fingerprint']
                or d['rules_version']!=app._settings(c)['rules_version']):raise CleanerError('profile_changed','Версия решения устарела',409)
        override=c.execute('''SELECT h.revision,h.needs_revalidation,o.verdict,o.fingerprint FROM cleaner_override_heads h
            JOIN cleaner_manual_overrides o ON o.account=h.account AND o.nm_id=h.nm_id AND o.query_hash=h.query_hash AND o.revision=h.revision
            WHERE h.account=? AND h.nm_id=? AND h.query_hash=?''',(app.key,obs['nm_id'],obs['query_hash'])).fetchone()
        if override:
            if (override['revision']!=d['override_revision'] or override['needs_revalidation'] or override['verdict']!='exclude'
                    or override['fingerprint']!=p.semantic_fingerprint):raise CleanerError('override_changed','Решение владельца изменилось',409)
        elif d['override_revision'] is not None:raise CleanerError('override_changed','Нет исходной версии решения',409)
        return dict(target=obs['target'],query=obs['query'],query_hash=obs['query_hash'],decision_id=d['decision_id'],
                    profile_version=p.version,fingerprint=p.semantic_fingerprint,rules_version=d['rules_version'],
                    override_revision=d['override_revision'],source=d['source'])

    def prepare(self,run_id,token,snapshot,candidates,membership):
        app=self.app;t=snapshot.target
        if not snapshot.complete or t.unsupported_reason:raise CleanerError('incomplete_snapshot','Нет полного поддержанного состояния')
        with app.store.transaction() as c:
            s,run=self._guard(c,run_id,token)
            if c.execute('SELECT 1 FROM cleaner_target_holds WHERE account=? AND target=?',(app.key,t.key)).fetchone():raise CleanerError('target_hold','Цель приостановлена',409)
            previous=c.execute("SELECT * FROM cleaner_write_operations WHERE account=? AND target=? AND state IN('prepared','dispatching','submitted','unresolved')",(app.key,t.key)).fetchone()
            if previous:
                if previous['state']!='prepared':raise CleanerError('target_unresolved','Предыдущая операция не разрешена',409)
                c.execute("UPDATE cleaner_write_operations SET state='cancelled_before_send',updated_at=? WHERE operation_id=?",(app.clock(),previous['operation_id']))
                app._event(c,'candidate_cancelled',dict(reason='fresh_preparation'),run_id=previous['run_id'],operation_id=previous['operation_id'])
            exact=[];scope={(r['target'],r['query_hash'],r['decision_id']) for r in json.loads(run['targets'])} if run['kind']=='manual_apply' else None
            for row in candidates:
                if row['target']!=t.key:raise CleanerError('wrong_target','Смешение целей запрещено')
                if scope is not None and (row['target'],row['query_hash'],row['decision_id']) not in scope:raise CleanerError('wrong_target','Вхождение не было в ручном задании')
                if row['query'] in snapshot.minus:
                    c.execute("UPDATE cleaner_observations SET state='observed_excluded',observed_state='excluded' WHERE account=? AND target=? AND query_hash=?",(app.key,t.key,row['query_hash']))
                    continue
                current=self._current(c,row)
                if snapshot.queries.get(row['query']) not in {'active','statistics'} or 'statistics' not in snapshot.sources.get(row['query'],()):
                    raise CleanerError('candidate_not_observed','Нет свежей точной статистики кандидата')
                exact.append(current)
            if not exact:
                app._event(c,'already_excluded',dict(target=t.key,new_confirmed=0),run_id=run_id);return None
            before=sorted(snapshot.minus);additions=sorted({r['query'] for r in exact});expected=sorted(set(before)|set(additions))
            if len(before)!=len(set(before)) or len(additions)!=len(exact):raise CleanerError('invalid_full_set','Повторная идентичность')
            if len(expected)>1000:raise CleanerError('minus_limit','Полный список превышает 1000 строк')
            versions=dict(settings_revision=s['revision'],rules_digest=app.rules_digest,items=sorted(exact,key=lambda r:r['query_hash']),
                          target=asdict(t),membership=list(membership),before_at=snapshot.observed_at,source='owner_decision' if run['kind']=='manual_apply' else 'automatic')
            basis=dict(account=app.key,target=t.key,run_id=run_id,before=before,expected=expected,additions=additions,versions=versions)
            op=new_id();now=app.clock()
            c.execute('''INSERT INTO cleaner_write_operations(operation_id,account,target,run_id,state,before_json,expected_json,additions,candidate_digest,versions,worker_token,worker_generation,created_at,updated_at)
                VALUES(?,?,?,?,'prepared',?,?,?,?,?,?,?,?,?)''',(op,app.key,t.key,run_id,canonical(before),canonical(expected),canonical(additions),digest(basis),canonical(versions),token,self.generation,now,now))
            for row in exact:
                c.execute("INSERT INTO cleaner_write_items(operation_id,query_hash,query,decision_id,override_revision,state) VALUES(?,?,?,?,?,'prepared')",(op,row['query_hash'],row['query'],row['decision_id'],row['override_revision']))
            app._event(c,'candidate_prepared',dict(target=t.key,additions=len(additions)),run_id=run_id,operation_id=op)
            return op

    def _cancel(self,operation,reason):
        with self.app.store.transaction() as c:
            row=c.execute('SELECT * FROM cleaner_write_operations WHERE operation_id=?',(operation,)).fetchone()
            if row and row['state']=='prepared':
                c.execute("UPDATE cleaner_write_operations SET state='cancelled_before_send',updated_at=? WHERE operation_id=?",(self.app.clock(),operation))
                self.app._event(c,'candidate_cancelled',dict(reason=reason),run_id=row['run_id'],operation_id=operation)

    def admit(self,operation,run_id,token,fresh,membership):
        app=self.app
        self.source.require_target_budget()
        with app.store.transaction() as c:
            s,run=self._guard(c,run_id,token)
            row=c.execute('SELECT * FROM cleaner_write_operations WHERE account=? AND operation_id=?',(app.key,operation)).fetchone()
            if not row or row['state']!='prepared' or row['run_id']!=run_id or row['worker_token']!=token or row['worker_generation']!=self.generation:
                raise CleanerError('cas_lost','Кандидат не принадлежит исполнителю',409)
            versions=json.loads(row['versions']);before=json.loads(row['before_json']);expected=json.loads(row['expected_json']);additions=json.loads(row['additions'])
            basis=dict(account=app.key,target=row['target'],run_id=run_id,before=before,expected=expected,additions=additions,versions=versions)
            if digest(basis)!=row['candidate_digest'] or expected!=sorted(set(before)|set(additions)) or not additions or len(expected)>1000:
                raise CleanerError('candidate_changed','Кандидат изменён',409)
            if s['revision']!=versions['settings_revision'] or versions['rules_digest']!=app.rules_digest:raise CleanerError('settings_changed','Настройки изменились',409)
            if c.execute('SELECT 1 FROM cleaner_target_holds WHERE account=? AND target=?',(app.key,row['target'])).fetchone():raise CleanerError('target_hold','Цель приостановлена',409)
            if not fresh.complete or fresh.target.unsupported_reason or asdict(fresh.target)!=versions['target'] or list(membership)!=versions['membership'] or sorted(fresh.minus)!=before:
                raise CleanerError('preflight_drift','Свежая цель изменилась',409)
            times=list(fresh.source_times.values())+[fresh.observed_at]
            if len(times)<4 or any(not 0<=(timestamp(app.clock())-timestamp(t)).total_seconds()<=self.preflight_max_age for t in times):raise CleanerError('preflight_stale','Предварительное чтение устарело',409)
            for item in versions['items']:
                if self._current(c,item)!=item:raise CleanerError('decision_changed','Версия решения изменилась',409)
                if fresh.queries.get(item['query']) not in {'active','statistics'} or 'statistics' not in fresh.sources.get(item['query'],()):raise CleanerError('candidate_not_observed','Кандидат больше не подтверждён свежей статистикой',409)
            ids=self.registry.prepare_search_cluster_operation_in_transaction(c,operation_id=operation,account=app.account,target=fresh.target,
                queries=additions,created_at=app.clock(),before_at=fresh.observed_at,provenance=basis,
                actor=app.owner_username if versions['source']=='owner_decision' else 'cleaner',source=versions['source'])
            for qh,item_id in ids.items():c.execute("UPDATE cleaner_write_items SET registry_item_id=?,state='dispatching' WHERE operation_id=? AND query_hash=?",(item_id,operation,qh))
            changed=c.execute("UPDATE cleaner_write_operations SET state='dispatching',dispatch_count=1,preflight_at=?,updated_at=? WHERE operation_id=? AND state='prepared' AND dispatch_count=0 AND worker_token=?",(fresh.observed_at,app.clock(),operation,token)).rowcount
            if changed!=1:raise CleanerError('cas_lost','Условный допуск потерян',409)
            c.execute('INSERT INTO cleaner_readback_jobs(operation_id,account) VALUES(?,?)',(operation,app.key))
            app._event(c,'dispatch_admitted',dict(target=row['target'],additions=len(additions),source=versions['source']),run_id=run_id,operation_id=operation)
            self.hook('before_seal',operation)
            self.session.seal(operation,row['target'],row['candidate_digest'])
            self.hook('before_commit',operation)
        self.session.committed(operation)
        self.hook('after_commit',operation)
        return expected

    def apply_target(self,run_id,token,snapshot,candidates):
        if not candidates:return None
        app=self.app;app.renew_lease(run_id,token,self.generation,phase='preparing')
        initial,membership=self.source.refresh_target(snapshot.target)
        if initial!=snapshot.target:raise CleanerError('target_changed','Состав цели изменился')
        op=self.prepare(run_id,token,snapshot,candidates,membership)
        if op is None:return None
        self.hook('after_prepare',op)
        try:
            app.renew_lease(run_id,token,self.generation,phase='preflight')
            target,members=self.source.refresh_target(snapshot.target)
            fresh=self.source.snapshot(target)
            app.renew_lease(run_id,token,self.generation,phase='waiting_rate_limit')
            slot=self.source.reserve_write() # all waits occur BEFORE the final CAS
            self.hook('before_admission',op)
            expected=self.admit(op,run_id,token,fresh,members)
        except CleanerError as exc:
            self._cancel(op,exc.code);raise
        self.hook('before_network',op)
        try:response=self.session.submit_once(op,lambda:self.source.set_minus_once(target,expected,slot))
        except (TimeoutError,ConnectionError,OSError):response=WriteResponse(None,0,'transport_ambiguous')
        self.hook('after_network',op)
        with app.store.transaction() as c:
            state='submitted' if response.status==200 else 'unresolved'
            c.execute("UPDATE cleaner_write_operations SET state=?,evidence=?,updated_at=? WHERE operation_id=? AND state='dispatching'",(state,canonical(asdict(response)),app.clock(),op))
            if response.retry_after:c.execute('UPDATE cleaner_readback_jobs SET retry_not_before=? WHERE operation_id=?',(plus_seconds(app.clock(),int(response.retry_after)+1),op))
            app._event(c,'write_response',dict(status=response.status,error=response.error),run_id=run_id,operation_id=op)
        self.hook('after_receipt',op)
        if response.status in {401,403}:raise CleanerError('unauthorized' if response.status==401 else 'forbidden','WB отказал всему аккаунту',403)
        return op


class CleanerReadback:
    def __init__(self,cleaner,source,*,generation,registry=None,hook=None):
        self.app,self.source,self.generation=cleaner,source,generation
        if source.account!=cleaner.account:raise CleanerError('account_mismatch','Источник другого аккаунта')
        self.registry=registry or ChangeRegistryRepository(cleaner.store.registry.runtime_dir)
        self.hook=hook or (lambda stage,op:None)

    def tick(self):
        app=self.app;job=app.claim_readback(generation=self.generation)
        if not job:return None
        with app.store.read() as c:op=dict(c.execute('SELECT * FROM cleaner_write_operations WHERE operation_id=?',(job['operation_id'],)).fetchone())
        target=Target(**json.loads(op['versions'])['target'])
        try:
            minus,observed=self.source.read_minus(target)
            self.hook('after_readback',op['operation_id'])
            return self.record(job,minus,observed)
        except CleanerError as exc:
            delay=max(20,getattr(exc,'retry_after',0))
            self.defer(job,exc.code,delay);return dict(operation_id=op['operation_id'],state='unresolved',reason=exc.code)
        except (TimeoutError,ConnectionError,OSError):
            self.defer(job,'readback_unavailable',20);return dict(operation_id=op['operation_id'],state='unresolved')

    def _job(self,c,job):
        row=c.execute('SELECT * FROM cleaner_readback_jobs WHERE account=? AND operation_id=?',(self.app.key,job['operation_id'])).fetchone()
        if (not row or row['state']!='running' or row['worker_token']!=job['worker_token'] or not row['lease_expires_at']
                or timestamp(row['lease_expires_at'])<=timestamp(self.app.clock()) or self.app._settings(c)['generation']!=self.generation):raise CleanerError('lease_lost','Право сверки потеряно',409)
        return row

    def _retry(self,attempt):return 20 if attempt%3 else 300

    def defer(self,job,reason,delay):
        with self.app.store.transaction() as c:
            row=self._job(c,job)
            c.execute("UPDATE cleaner_readback_jobs SET state='queued',worker_token=NULL,lease_expires_at=NULL,retry_not_before=? WHERE operation_id=?",(plus_seconds(self.app.clock(),int(max(delay,self._retry(row['attempts'])))+1),job['operation_id']))
            c.execute("UPDATE cleaner_write_operations SET state='unresolved',updated_at=? WHERE operation_id=? AND state IN('dispatching','submitted','unresolved')",(self.app.clock(),job['operation_id']))
            op=c.execute('SELECT run_id FROM cleaner_write_operations WHERE operation_id=?',(job['operation_id'],)).fetchone()
            self.app._event(c,'readback_deferred',dict(reason=reason,attempt=row['attempts']),run_id=op[0],operation_id=job['operation_id'])

    def record(self,job,minus,observed):
        app=self.app
        if len(minus)!=len(set(minus)):raise CleanerError('minus_duplicate_query','Неполный ответ')
        for q in minus:query_hash(q)
        if timestamp(observed)>timestamp(app.clock()):raise CleanerError('readback_clock','Время чтения в будущем')
        with app.store.transaction() as c:
            row=self._job(c,job)
            op=c.execute('SELECT * FROM cleaner_write_operations WHERE account=? AND operation_id=?',(app.key,job['operation_id'])).fetchone()
            if op['state'] not in {'dispatching','submitted','unresolved'}:raise CleanerError('readback_not_applicable','Операция уже завершена')
            if timestamp(observed)<timestamp(op['preflight_at']):raise CleanerError('readback_stale','Чтение старше допуска')
            before=set(json.loads(op['before_json']));expected=set(json.loads(op['expected_json']));added=set(json.loads(op['additions']));actual=set(minus)
            missing_old=sorted(before-actual);extra=sorted(actual-expected);present=sorted(added&actual);missing=sorted(added-actual)
            evidence=dict(minus=sorted(actual),missing_old=missing_old,extra=extra,present=present,missing=missing,observed_at=observed)
            run=c.execute('SELECT * FROM cleaner_runs WHERE run_id=?',(op['run_id'],)).fetchone();late=bool(run['scan_finished_at'])
            previous={r[0] for r in c.execute("SELECT query_hash FROM cleaner_write_items WHERE operation_id=? AND confirmed_at IS NOT NULL",(op['operation_id'],))}
            facts=self.registry.confirm_search_cluster_items_in_transaction(c,operation_id=op['operation_id'],present_queries=present,observed_at=observed,before_at=op['preflight_at'],evidence=evidence)
            newly=set(facts)-previous
            for qh in facts:
                c.execute("UPDATE cleaner_write_items SET state='confirmed',confirmed_at=coalesce(confirmed_at,?) WHERE operation_id=? AND query_hash=?",(observed,op['operation_id'],qh))
                c.execute("UPDATE cleaner_observations SET state='confirmed',observed_state='excluded' WHERE account=? AND target=? AND query_hash=?",(app.key,op['target'],qh))
            if missing_old or extra:
                c.execute('INSERT OR IGNORE INTO cleaner_target_holds VALUES(?,?,?,?)',(app.key,op['target'],'readback_drift',app.clock()))
            complete=actual==expected and not (missing_old or extra)
            state='confirmed' if complete else 'unresolved'
            c.execute('UPDATE cleaner_write_operations SET state=?,updated_at=?,evidence=? WHERE operation_id=?',(state,app.clock(),canonical(evidence),op['operation_id']))
            c.execute("UPDATE cleaner_readback_jobs SET state=?,worker_token=NULL,lease_expires_at=NULL,retry_not_before=? WHERE operation_id=?",('done' if complete else 'queued',None if complete else plus_seconds(app.clock(),self._retry(row['attempts'])),op['operation_id']))
            source=json.loads(op['versions'])['source'];counts=dict(confirmed_automatic=len(newly) if source=='automatic' else 0,confirmed_manual=len(newly) if source=='owner_decision' else 0)
            app._event(c,'late_confirmation' if late else 'readback_result',dict(target=op['target'],state=state,late=late,**counts,missing=len(missing),missing_old=len(missing_old),extra=len(extra)),run_id=op['run_id'],operation_id=op['operation_id'])
            if complete:
                unsettled=c.execute("SELECT 1 FROM cleaner_write_operations WHERE run_id=? AND state IN('prepared','dispatching','submitted','unresolved')",(op['run_id'],)).fetchone()
                if not unsettled and late:c.execute('UPDATE cleaner_runs SET settled_at=coalesce(settled_at,?) WHERE run_id=?',(app.clock(),op['run_id']))
            return dict(operation_id=op['operation_id'],state=state,confirmed=len(present),newly_confirmed=len(newly),late=late,missing_old=missing_old,extra=extra)
