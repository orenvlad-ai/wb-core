"""Explicit worker tick. Optional guarded writer; no thread, service or timer."""
from __future__ import annotations
import json
from contextlib import nullcontext
import time
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.contracts.search_cluster_cleaner import CleanerError, ReadSource, Target


class CleanerWorker:
    def __init__(self, cleaner: KeywordCleaner, source: ReadSource, *, generation: str,
                 max_seconds: float = 1800, max_targets: int = 10000, monotonic=time.monotonic,
                 writer=None,readback=None):
        self.cleaner,self.source,self.generation=cleaner,source,generation
        self.max_seconds,self.max_targets,self.monotonic=max_seconds,max_targets,monotonic
        self.writer,self.readback=writer,readback

    def _apply(self,run,token,snapshot):
        if not self.writer:return
        candidates=[r for r in self.cleaner.pending_candidates(run) if r['target']==snapshot.target.key]
        self.writer.apply_target(run,token,snapshot,candidates)
        if self.readback:self._readback()

    def _readback(self):
        result=self.readback.tick()
        if result and result.get('reason') in {'unauthorized','forbidden','account_mismatch'}:
            raise CleanerError(result['reason'],'Аккаунт WB недоступен',403)
        return result

    def tick(self) -> dict | None:
        app=self.cleaner;app.heartbeat(generation=self.generation)
        if self.readback:
            try:self._readback() # independent of enabled/account slot
            except CleanerError as exc:return dict(state='held',reason=exc.code)
        app.scheduler_tick();run=app.claim_run(generation=self.generation)
        if not run:return None
        rid,token=run['run_id'],run['worker_token']
        with app.store.transaction() as c:
            app._lease(c,rid,token,self.generation)
            c.execute('UPDATE cleaner_runs SET transport_enabled=? WHERE run_id=?',(int(self.writer is not None),rid))
        start=self.monotonic();attempted=0;reason='';remaining=[]
        try:
            if run['kind']=='manual_apply':
                candidates=app.pending_candidates(rid)
                if not self.writer:
                    with app.store.transaction() as c:
                        app._lease(c,rid,token,self.generation)
                        app._event(c,'dry_run_manual_candidates',dict(count=len(candidates)),run_id=rid)
                    return app.finish_run(rid,token,self.generation)
                groups={}
                for row in candidates:groups.setdefault(row['target'],[]).append(row)
                for key,items in groups.items():
                    if self.monotonic()-start>=self.max_seconds or attempted>=self.max_targets:
                        remaining.extend(dict(target=r['target'],query_hash=r['query_hash'],decision_id=r['decision_id']) for r in items);continue
                    attempted+=1
                    target=Target(items[0]['advert_id'],items[0]['nm_id'],contract_verified=True)
                    try:
                        app.renew_lease(rid,token,self.generation,phase='reading')
                        with self.source.target_attempt():
                            target,_=self.source.refresh_target(target)
                            snapshot=self.source.snapshot(target)
                            # Manual scope is pinned to its original decision.
                            self.writer.apply_target(rid,token,snapshot,items)
                        if self.readback:self._readback()
                        with app.store.transaction() as c:
                            app._lease(c,rid,token,self.generation)
                            app._record_target(c,rid,target,'done',snapshot.complete,'',snapshot.observed_at,{},snapshot.source_times)
                    except CleanerError as exc:
                        if exc.code in {'disabled','lease_lost','generation_conflict'}:raise
                        app.record_target_error(rid,token,self.generation,target,exc.code)
                        if exc.code in {'unauthorized','forbidden','account_mismatch','external_hold','restore_journal_gap','target_budget','read_budget'}:
                            reason=exc.code;remaining=json.loads(run['targets']);break
                if remaining and not reason:reason='apply_budget_reached'
                return app.finish_run(rid,token,self.generation,reason=reason,remaining=remaining)
            targets,errors=self.source.catalog()
            app.sync_catalog(rid,token,self.generation,targets,errors)
            while self.monotonic()-start<self.max_seconds and attempted<self.max_targets:
                app.scheduler_tick();target=app.next_scan_target(rid,token,self.generation)
                if target is None:break
                app.renew_lease(rid,token,self.generation,phase='reading');attempted+=1
                if target.unsupported_reason:
                    app.record_target_error(rid,token,self.generation,target,target.unsupported_reason);continue
                try:
                    with self.source.target_attempt() if hasattr(self.source,'target_attempt') else nullcontext():
                        snapshot=self.source.snapshot(target)
                        if snapshot.target!=target:raise CleanerError('wrong_target','Ответ относится к другой цели')
                        app.record_snapshot(rid,token,self.generation,snapshot)
                        if snapshot.complete:self._apply(rid,token,snapshot)
                except CleanerError as exc:
                    if exc.code in {'lease_lost','generation_conflict','disabled'}:raise
                    app.record_target_error(rid,token,self.generation,target,exc.code,retry_after_seconds=int(getattr(exc,'retry_after',0)))
                    if exc.code in {'unauthorized','account_mismatch','forbidden','external_hold','restore_journal_gap'}:
                        reason=exc.code;break
                except (TimeoutError,ConnectionError,OSError):
                    app.record_target_error(rid,token,self.generation,target,'source_temporarily_unavailable',retry_after_seconds=60)
            if (self.monotonic()-start>=self.max_seconds or attempted>=self.max_targets) and app.next_scan_target(rid,token,self.generation) is not None:reason='scan_budget_reached'
            return app.finish_run(rid,token,self.generation,reason=reason)
        except CleanerError as exc:
            if exc.code=='disabled':
                if run['kind']=='manual_apply':remaining=json.loads(run['targets'])
                return app.finish_run(rid,token,self.generation,reason='disabled',remaining=remaining)
            if exc.code in {'lease_lost','generation_conflict'}:return dict(run_id=rid,state='lease_lost')
            return app.finish_run(rid,token,self.generation,reason=exc.code,remaining=remaining)
        except (TimeoutError,ConnectionError,OSError):
            return app.finish_run(rid,token,self.generation,reason='catalog_unavailable',remaining=remaining)


def product_tick(cleaner,source,admission,*,generation,**options):
    """Explicit product composition, including readback when writer is held.

    Schema installation is a separate setup/release action, never part of tick.
    """
    from packages.application.search_cluster_cleaner_writer import CleanerWriter,CleanerReadback
    readback=CleanerReadback(cleaner,source,generation=generation)
    try:
        with admission.session(account=cleaner.account,generation=generation) as session:
            with cleaner.store.transaction() as c:
                c.execute('UPDATE cleaner_settings SET transport_enabled=1 WHERE account=?',(cleaner.key,))
            writer=CleanerWriter(cleaner,source,session,generation=generation)
            return CleanerWorker(cleaner,source,generation=generation,writer=writer,readback=readback,**options).tick()
    except CleanerError as exc:
        if exc.code not in {'external_hold','restore_journal_gap','writer_alive'}:raise
        with cleaner.store.transaction() as c:
            c.execute('UPDATE cleaner_settings SET transport_enabled=0 WHERE account=?',(cleaner.key,))
            cleaner._event(c,'admission_hold',dict(reason=exc.code))
        result=readback.tick()
        return dict(state='held',reason=exc.code,readback=result)
