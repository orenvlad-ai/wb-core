"""Explicit worker tick. Optional guarded writer; no thread, service or timer."""
from __future__ import annotations
import json
from contextlib import nullcontext
import time
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.contracts.search_cluster_cleaner import CleanerError, ReadSource, Target, digest


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


class ManualCleanerWorker:
    """One explicit scope; it never invokes scheduler_tick or FIFO claim_run."""
    def __init__(self, cleaner: KeywordCleaner, source: ReadSource, guard, *, generation: str):
        self.cleaner,self.source,self.guard,self.generation=cleaner,source,guard,generation

    @staticmethod
    def _scope(run_id, targets):
        return digest(dict(run_id=run_id,targets=sorted(t.key for t in targets)))

    def _read(self, targets):
        snapshots=[]
        for requested in targets:
            with self.source.target_attempt():
                target,membership=self.source.refresh_target(requested)
                if target.key!=requested.key:raise CleanerError('wrong_target','Ответ WB относится к другой цели',409)
                snapshot=self.source.snapshot(target)
                if snapshot.target!=target:raise CleanerError('wrong_target','Снимок WB относится к другой цели',409)
                snapshots.append((target,membership,snapshot))
        return snapshots

    @staticmethod
    def _prestate_digest(run_id, snapshots):
        value=dict(run_id=run_id,targets=[dict(target=t.key,membership=list(m),minus=sorted(s.minus),complete=s.complete) for t,m,s in snapshots])
        return 'sha256:'+digest(value)

    def preview(self, *, run_id: str, targets: list[Target]) -> dict:
        if not targets or len({t.key for t in targets}) != len(targets):
            raise CleanerError('manual_scope_invalid','Нужна точная область ручной проверки',422)
        with self.cleaner.store.read() as c:
            settings=self.cleaner._settings(c)
            run=c.execute('SELECT * FROM cleaner_runs WHERE account=? AND run_id=?',(self.cleaner.key,run_id)).fetchone()
            if settings['enabled'] or not settings['baseline_ready'] or not run or run['state'] != 'queued':
                raise CleanerError('manual_run_not_ready','Ручное задание недоступно',409)
            if c.execute("SELECT 1 FROM cleaner_runs WHERE account=? AND run_id<>? AND state IN('queued','accepted','running')",(self.cleaner.key,run_id)).fetchone():
                raise CleanerError('manual_queue_blocked','Есть другое незавершённое задание',409)
            declared={str(v.get('target') or '') for v in json.loads(run['targets'])}
            if declared != {t.key for t in targets}: raise CleanerError('manual_scope_drift','Область ручного запуска изменилась',409)
            candidates=[dict(v) for v in self.cleaner.pending_candidates(run_id)] if run['kind']=='manual_apply' else []
        snapshots=self._read(targets)
        prestate=dict(run_id=run_id,targets=[dict(target=t.key,membership=list(m),minus=sorted(s.minus),complete=s.complete) for t,m,s in snapshots])
        candidate=dict(prestate=prestate,kind=run['kind'],candidates=[dict(target=v['target'],query_hash=v['query_hash'],decision_id=v['decision_id'],execution_eligibility=v['execution_eligibility']) for v in candidates])
        return dict(run_id=run_id,target=','.join(sorted(t.key for t in targets)),scope=dict(target_count=len(targets),kind=run['kind']),prestate_sha256='sha256:'+digest(prestate),candidate_sha256='sha256:'+digest(candidate),recovery=dict(kind='held_manual_capability',scope_digest=self._scope(run_id,targets)))

    def execute(self, *, run_id: str, targets: list[Target], expected_prestate: str, expected_candidate: str, production_operation_id: str, reviewed_candidate: str|None=None) -> dict:
        preview=self.preview(run_id=run_id,targets=targets)
        if preview['prestate_sha256'] != expected_prestate or preview['candidate_sha256'] != expected_candidate:
            raise CleanerError('manual_preview_drift','Свежая ручная проверка изменилась',409)
        run=self.cleaner.claim_exact_manual_run(run_id=run_id,targets=targets,generation=self.generation)
        token=run['worker_token'];scope_digest=self._scope(run_id,targets)
        with self.cleaner.store.transaction() as c:
            self.cleaner._lease(c,run_id,token,self.generation)
            self.cleaner._event(c,'stage_e_manual_binding',dict(operation_id=production_operation_id,targets=sorted(t.key for t in targets),prestate_sha256=expected_prestate,candidate_sha256=reviewed_candidate or expected_candidate),run_id=run_id)
        if run['kind']=='scan':
            try:
                snapshots=self._read(targets)
                if self._prestate_digest(run_id,snapshots)!=expected_prestate:raise CleanerError('manual_preview_drift','Состояние WB изменилось после допуска',409)
                for _target,_members,snapshot in snapshots:
                    self.cleaner.record_snapshot(run_id,token,self.generation,snapshot,manual_only=True)
                return self.cleaner.finish_run(run_id,token,self.generation,manual_only=True)
            except CleanerError as exc:
                return self.cleaner.finish_run(run_id,token,self.generation,reason=exc.code,manual_only=True)
            except (TimeoutError,ConnectionError,OSError):
                return self.cleaner.finish_run(run_id,token,self.generation,reason='source_temporarily_unavailable',manual_only=True)
        with self.guard.manual_session(account=self.cleaner.account,generation=self.generation,capability=dict(run_id=run_id,targets=sorted(t.key for t in targets),scope_digest=scope_digest,production_operation_id=production_operation_id,prestate_sha256=expected_prestate,candidate_sha256=reviewed_candidate or expected_candidate)) as session:
            self.cleaner.set_manual_restore_hold(held=False,generation=self.generation)
            self.cleaner.set_manual_transport(enabled=True,generation=self.generation)
            try:
                from packages.application.search_cluster_cleaner_writer import CleanerWriter, CleanerReadback
                writer=CleanerWriter(self.cleaner,self.source,session,generation=self.generation,manual_only=True)
                operations=[]
                snapshots=self._read(targets)
                if self._prestate_digest(run_id,snapshots)!=expected_prestate:raise CleanerError('manual_preview_drift','Состояние WB изменилось после допуска',409)
                for _target,_members,snapshot in snapshots:
                    candidates=[v for v in self.cleaner.pending_candidates(run_id) if v['target']==snapshot.target.key]
                    operation=writer.apply_target(run_id,token,snapshot,candidates)
                    if operation:operations.append(operation)
                # One immediate readback is safe. Any unavailable result remains
                # durable for the separate readback-only production action.
                readback=CleanerReadback(self.cleaner,self.source,generation=self.generation)
                for operation in operations:readback.tick(operation_id=operation)
            finally:
                self.cleaner.close_manual_window()
        return self.cleaner.finish_run(run_id,token,self.generation,manual_only=True)


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
