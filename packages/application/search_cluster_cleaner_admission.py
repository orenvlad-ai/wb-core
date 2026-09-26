"""Server-owned dispatch fence, outside the business backup and closed by default.

No environment flag grants admission. Activation is an explicit recovery/release
step after the operational baseline and all external dispatch seals reconcile.
A seal is fsynced before the operational commit: any uncertainty sacrifices
availability, never reissues an old network right after restoration.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import uuid
from packages.contracts.search_cluster_cleaner import CleanerError, canonical


def process_identity(pid=None):
    pid=pid or os.getpid()
    try:
        stat=Path(f'/proc/{pid}/stat')
        if stat.exists():
            fields=stat.read_text().rsplit(')',1)[1].split()
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return dict(pid=pid,start=boot+':'+fields[19])
        result=subprocess.run(['ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=3)
        return dict(pid=pid,start=result.stdout.strip()) if result.returncode==0 and result.stdout.strip() else None
    except (OSError,subprocess.SubprocessError):
        raise CleanerError('process_identity_unknown','Не удалось проверить старый процесс',409)


class AdmissionGuard:
    def __init__(self, directory: Path, store):
        self.directory=Path(directory).resolve();self.store=store
        if self.directory.is_relative_to(store.registry.runtime_dir) or store.registry.runtime_dir.is_relative_to(self.directory):
            raise CleanerError('admission_path_invalid','Допуск должен храниться отдельно от business backup')
        self.path=self.directory/'admission.json';self.lockpath=self.directory/'writer.lock'

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.lockpath.open('a+b') as handle:
            try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise CleanerError('writer_alive','Предыдущий writer ещё работает',409)
            try: yield
            finally: fcntl.flock(handle,fcntl.LOCK_UN)

    def _load(self):
        try:
            state=json.loads(self.path.read_text())
            if (state['version']!=1 or type(state['hold']) is not bool or not isinstance(state['seals'],dict)
                    or not state['generation'] or not state['account']): raise ValueError()
            return state
        except (OSError,ValueError,KeyError,TypeError):
            raise CleanerError('external_hold','Внешний допуск отсутствует или повреждён',409)

    def _save(self,state):
        temp=self.directory/('admission.'+uuid.uuid4().hex+'.tmp')
        try:
            with temp.open('x',encoding='utf-8') as out:
                os.chmod(temp,0o600);out.write(canonical(state));out.flush();os.fsync(out.fileno())
            os.replace(temp,self.path)
            fd=os.open(self.directory,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if temp.exists():temp.unlink()

    def _previous_stopped(self,state):
        old=state.get('owner')
        if old and process_identity(old['pid'])==old:
            raise CleanerError('writer_alive','Старый PID и время старта ещё существуют',409)

    def _reconcile(self,state,conn):
        for operation,seal in state['seals'].items():
            row=conn.execute('SELECT account,target,candidate_digest,dispatch_count FROM cleaner_write_operations WHERE operation_id=?',(operation,)).fetchone()
            if not row or row['dispatch_count']!=1 or row['account']!=state['account'] or row['target']!=seal['target'] or row['candidate_digest']!=seal['digest']:
                raise CleanerError('restore_journal_gap','Восстановленная копия не содержит всех допусков; требуется сверка',409)

    def activate(self,*,account,generation,evidence):
        """Explicit server release/recovery entry; never called from web/env/tick.

        Evidence is a reviewable receipt reference. Existing seals cannot be
        erased/replaced; a lost interval requires separate recovery, outside v1.
        """
        if not isinstance(evidence,str) or not evidence.strip():raise CleanerError('recovery_evidence_required','Нужно основание допуска')
        with self._lock():
            if self.path.exists():
                state=self._load();self._previous_stopped(state)
                if state['account']!=account.key:raise CleanerError('account_mismatch','Иной аккаунт допуска',409)
            else:state=dict(version=1,hold=True,generation=generation,account=account.key,seals={},owner=None)
            with self.store.read() as c:
                settings=c.execute('SELECT * FROM cleaner_settings WHERE account=?',(account.key,)).fetchone()
                if not settings or settings['generation']!=generation or settings['restore_hold'] or not settings['baseline_ready']:
                    raise CleanerError('restore_hold','Не подтверждены поколение и исходная база',409)
                self._reconcile(state,c)
            state.update(generation=generation,hold=False,owner=None,evidence=evidence)
            self._save(state)

    def hold(self,*,reason):
        with self._lock():
            state=self._load();self._previous_stopped(state)
            state.update(hold=True,reason=reason);self._save(state)

    def initialize_held(self, *, account, generation, evidence):
        """Create or verify the closed external fence during explicit setup."""
        if not isinstance(evidence,str) or not evidence.strip():
            raise CleanerError('recovery_evidence_required','Нужно основание закрытого допуска')
        with self._lock():
            if self.path.exists():
                state=self._load();self._previous_stopped(state)
                if state['account']!=account.key or state['generation']!=generation or not state['hold']:
                    raise CleanerError('admission_state_conflict','Внешний допуск не соответствует bootstrap',409)
                return
            self._save(dict(version=1,hold=True,generation=generation,account=account.key,seals={},owner=None,reason='stage_e_bootstrap',evidence=evidence))

    def recover_manual_capability(self, *, account, generation, production_operation_id, terminal_operation_ids):
        """Clear only a dead owner's fully read-back manual capability."""
        with self._lock():
            state=self._load()
            if state['account']!=account.key or state['generation']!=generation or not state['hold']:
                raise CleanerError('external_hold','Внешний допуск изменился',409)
            self._previous_stopped(state)
            capability=state.get('manual_capability')
            if (not isinstance(capability,dict) or capability.get('production_operation_id')!=production_operation_id
                    or capability.get('operation_id') not in set(terminal_operation_ids)):
                raise CleanerError('manual_capability_lost','Ручной допуск не готов к восстановлению',409)
            with self.store.read() as c:self._reconcile(state,c)
            state.pop('manual_capability',None);state['owner']=None;state['reason']='manual_readback_recovered';self._save(state)

    def recover_empty_manual_capability(self, *, account, generation, production_operation_id, run_id):
        """Close a dead exact capability that never minted a dispatch right."""
        with self._lock():
            state=self._load()
            if state['account']!=account.key or state['generation']!=generation or not state['hold']:
                raise CleanerError('external_hold','Внешний допуск изменился',409)
            self._previous_stopped(state)
            capability=state.get('manual_capability')
            if (not isinstance(capability,dict) or capability.get('production_operation_id')!=production_operation_id
                    or capability.get('run_id')!=run_id or capability.get('operation_id')):
                raise CleanerError('manual_capability_lost','Ручной допуск не готов к восстановлению',409)
            with self.store.read() as c:
                self._reconcile(state,c)
                if c.execute('SELECT 1 FROM cleaner_write_operations WHERE account=? AND run_id=?',(account.key,run_id)).fetchone():
                    raise CleanerError('manual_operation_exists','Ручная операция требует сверки WB',409)
                bindings=[json.loads(row[0]) for row in c.execute("SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_manual_binding'",(account.key,run_id))]
                if not any(row.get('operation_id')==production_operation_id for row in bindings):
                    raise CleanerError('manual_binding_missing','Нет точной привязки ручного запуска',409)
            state.pop('manual_capability',None);state['owner']=None;state['reason']='manual_no_submit_recovered';self._save(state)

    def recover_unsubmitted_manual_run(self, *, cleaner, generation, production_operation_id, run_id):
        """Requeue an expired exact run only when no WB dispatch right existed.

        The external lock fences a new manual session while the operational
        rollback and capability cleanup are reconciled. Prior seals remain
        immutable and are all checked against SQLite before any change.
        """
        from packages.application.search_cluster_cleaner import timestamp
        account=cleaner.account
        with self._lock():
            state=self._load()
            if state['account']!=account.key or state['generation']!=generation or not state['hold']:
                raise CleanerError('external_hold','Внешний допуск изменился',409)
            self._previous_stopped(state)
            capability=state.get('manual_capability')
            if capability and (not isinstance(capability,dict) or capability.get('run_id')!=run_id
                    or capability.get('production_operation_id')!=production_operation_id or capability.get('operation_id')):
                raise CleanerError('manual_capability_lost','Ручной допуск требует отдельной сверки',409)
            with cleaner.store.transaction() as c:
                self._reconcile(state,c)
                settings=cleaner._settings(c)
                run=c.execute('SELECT * FROM cleaner_runs WHERE account=? AND run_id=?',(account.key,run_id)).fetchone()
                if not run or settings['generation']!=generation or settings['enabled'] or run['trigger'] not in {'manual_exact','manual_exact_candidates'}:
                    raise CleanerError('manual_recovery_scope_mismatch','Ручное задание изменилось',409)
                bindings=[json.loads(row[0]) for row in c.execute(
                    "SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_manual_binding'",
                    (account.key,run_id))]
                declared={str(row.get('target') or '') for row in json.loads(run['targets'])}
                if (not bindings or len(declared)!=1 or any(row.get('operation_id')!=production_operation_id
                        or set(row.get('targets') or [])!=declared for row in bindings)):
                    raise CleanerError('manual_binding_missing','Точная привязка запуска не подтверждена',409)
                operations=c.execute('SELECT operation_id,target,state,dispatch_count,worker_token,worker_generation FROM cleaner_write_operations WHERE account=? AND run_id=?',
                                     (account.key,run_id)).fetchall()
                if (any(row['operation_id'] in state['seals'] or row['dispatch_count']!=0
                        or row['target'] not in declared or row['state'] not in {'prepared','cancelled_before_send'} for row in operations)
                        or any(c.execute('SELECT 1 FROM cleaner_readback_jobs WHERE operation_id=?',(row['operation_id'],)).fetchone() for row in operations)):
                    raise CleanerError('manual_dispatch_uncertain','Отправка требует чтения результата WB',409)
                recovery=[(row['sequence'],json.loads(row['facts'])) for row in c.execute(
                    "SELECT sequence,facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_unsent_recovered'",
                    (account.key,run_id))]
                matching_recovery=[(sequence,event) for sequence,event in recovery
                                   if event.get('production_operation_id')==production_operation_id]
                recovered_ids={op for _,event in matching_recovery
                               for op in event.get('cancelled_operations',[])}
                latest_recovery=max((sequence for sequence,_ in matching_recovery),default=0)
                newly_cancelled=[]
                for row in operations:
                    if row['state']!='cancelled_before_send' or row['operation_id'] in recovered_ids:continue
                    # A later failed fresh preflight can cancel a replacement
                    # operation. Accept it only when both local preparation and
                    # cancellation were durably recorded after the prior exact
                    # recovery. The seal/readback/dispatch checks above still
                    # apply to every operation in this run.
                    events=c.execute("SELECT sequence,kind FROM cleaner_events WHERE account=? AND run_id=? AND operation_id=? AND kind IN('candidate_prepared','candidate_cancelled') ORDER BY sequence",
                                     (account.key,run_id,row['operation_id'])).fetchall()
                    prepared=next((event['sequence'] for event in events if event['kind']=='candidate_prepared' and event['sequence']>latest_recovery),None)
                    cancelled=next((event['sequence'] for event in events if event['kind']=='candidate_cancelled' and prepared and event['sequence']>prepared),None)
                    if not matching_recovery or not prepared or not cancelled:
                        raise CleanerError('manual_recovery_scope_mismatch','История подготовленной записи не подтверждена',409)
                    newly_cancelled.append(row['operation_id'])
                certified_ids=recovered_ids|set(newly_cancelled)
                if any(row['state']=='cancelled_before_send' and row['operation_id'] not in certified_ids for row in operations):
                    raise CleanerError('manual_recovery_scope_mismatch','История подготовленной записи не подтверждена',409)
                if any(c.execute("SELECT 1 FROM cleaner_write_items WHERE operation_id=? AND (state!='cancelled_before_send' OR confirmed_at IS NOT NULL OR registry_item_id IS NOT NULL)",
                                 (row['operation_id'],)).fetchone() for row in operations if row['state']=='cancelled_before_send' and row['operation_id'] in recovered_ids):
                    raise CleanerError('manual_recovery_scope_mismatch','История фраз подготовленной записи не подтверждена',409)
                if any(c.execute("SELECT 1 FROM cleaner_write_items WHERE operation_id=? AND (state NOT IN('prepared','cancelled_before_send') OR confirmed_at IS NOT NULL OR registry_item_id IS NOT NULL)",
                                 (operation_id,)).fetchone() for operation_id in newly_cancelled):
                    raise CleanerError('manual_recovery_scope_mismatch','История фраз новой подготовки не подтверждена',409)
                for operation_id in newly_cancelled:
                    c.execute("UPDATE cleaner_write_items SET state='cancelled_before_send' WHERE operation_id=? AND state='prepared'",(operation_id,))
                prepared=[row for row in operations if row['state']=='prepared']
                if (run['state']=='queued' and matching_recovery and not prepared and not run['worker_token']
                        and not run['started_at'] and not run['lease_expires_at'] and run['worker_generation'] is None):
                    pass  # DB committed before an interrupted guard cleanup.
                elif (run['state']=='running' and run['lease_expires_at']
                      and run['worker_token'] and run['worker_generation']==generation
                      and timestamp(run['lease_expires_at'])<=timestamp(cleaner.clock())
                      and (run['kind']!='scan' or not c.execute('SELECT 1 FROM cleaner_run_targets WHERE run_id=?',(run_id,)).fetchone())):
                    if any(row['worker_token']!=run['worker_token'] or row['worker_generation']!=generation for row in prepared):
                        raise CleanerError('manual_recovery_scope_mismatch','Подготовленная запись изменилась',409)
                    if run['kind']=='manual_apply' and prepared:
                        wanted={(row['target'],row['query_hash'],row['decision_id']) for row in json.loads(run['targets'])}
                        items=[(op,item) for op in prepared for item in c.execute(
                            'SELECT query_hash,decision_id,state,confirmed_at,registry_item_id FROM cleaner_write_items WHERE operation_id=?',
                            (op['operation_id'],))]
                        if (len(prepared)!=1 or len(items)!=len(wanted) or
                                any(item['state']!='prepared' or item['confirmed_at'] or item['registry_item_id'] for _,item in items) or
                                {(op['target'],item['query_hash'],item['decision_id']) for op,item in items}!=wanted):
                            raise CleanerError('manual_recovery_scope_mismatch','Состав подготовленных фраз изменился',409)
                    elif prepared:
                        raise CleanerError('manual_recovery_scope_mismatch','В проверке появились подготовленные записи',409)
                    for row in prepared:
                        c.execute("UPDATE cleaner_write_items SET state='cancelled_before_send' WHERE operation_id=?",(row['operation_id'],))
                        c.execute("UPDATE cleaner_write_operations SET state='cancelled_before_send',updated_at=? WHERE operation_id=? AND state='prepared' AND dispatch_count=0",
                                  (cleaner.clock(),row['operation_id']))
                    c.execute("UPDATE cleaner_runs SET state='queued',phase='queued',worker_token=NULL,worker_generation=NULL,lease_expires_at=NULL,started_at=NULL,transport_enabled=0 WHERE run_id=? AND state='running'",
                              (run_id,))
                    c.execute('UPDATE cleaner_settings SET restore_hold=1,transport_enabled=0 WHERE account=?',(account.key,))
                    cleaner._event(c,'stage_e_unsent_recovered',dict(production_operation_id=production_operation_id,
                                  cancelled_operations=[row['operation_id'] for row in prepared]+newly_cancelled,
                                  kind=run['kind']),run_id=run_id)
                else:
                    raise CleanerError('manual_recovery_not_ready','Право исполнения ещё не истекло',409)
            if capability or state.get('owner'):
                state.pop('manual_capability',None);state['owner']=None;state['reason']='manual_unsent_recovered';self._save(state)
            return True

    @contextmanager
    def session(self,*,account,generation,manual_capability=None):
        with self._lock():
            state=self._load();self._previous_stopped(state)
            manual=manual_capability is not None
            capability=state.get('manual_capability')
            if (state['hold'] and not manual) or state['generation']!=generation or state['account']!=account.key:
                raise CleanerError('external_hold','Поколение не допущено',409)
            if manual and (not isinstance(capability,dict) or capability != manual_capability):
                raise CleanerError('manual_capability_lost','Одноразовый ручной допуск утрачен',409)
            with self.store.read() as c:self._reconcile(state,c)
            identity=process_identity()
            if not identity:raise CleanerError('process_identity_unknown','Нет точной идентичности процесса')
            state['owner']=identity;self._save(state)
            session=AdmissionSession(self,state,identity,manual=manual)
            try:yield session
            finally:
                session.active=False
                current=self._load()
                if current.get('owner')==identity:
                    current['owner']=None
                    if manual:
                        current.pop('manual_capability',None)
                        current.update(hold=True,reason='manual_capability_completed')
                    self._save(current)

    @contextmanager
    def manual_session(self,*,account,generation,capability):
        """One exact held-mode capability. The global guard never opens.

        A crash leaves the hold set and the durable seals intact.  It therefore
        blocks another submit until an explicit readback/recovery procedure.
        """
        if not isinstance(capability,dict) or not capability.get('run_id') or not capability.get('scope_digest'):
            raise CleanerError('manual_capability_invalid','Нет точного ручного допуска',422)
        with self._lock():
            state=self._load();self._previous_stopped(state)
            if not state['hold'] or state['generation']!=generation or state['account']!=account.key:
                raise CleanerError('external_hold','Ручной допуск требует закрытый внешний контур',409)
            if state.get('manual_capability'):
                raise CleanerError('manual_capability_stale','Предыдущий ручной допуск требует сверки',409)
            with self.store.read() as c:self._reconcile(state,c)
            state['manual_capability']=dict(capability)
            state['reason']='manual_capability_active'
            self._save(state)
        with self.session(account=account,generation=generation,manual_capability=dict(capability)) as session:
            yield session


class AdmissionSession:
    def __init__(self,guard,state,identity,*,manual=False):
        self.guard,self.state,self.identity,self.manual=guard,state,identity,manual;self.active=True;self.rights=set();self._sealed_here=set()

    def check(self,conn,account,generation):
        current=self.guard._load()
        if (not self.active or process_identity()!=self.identity or current!=self.state
                or (current['hold'] and not self.manual) or current['account']!=account.key or current['generation']!=generation):
            raise CleanerError('external_hold','Процесс утратил внешний допуск',409)
        self.guard._reconcile(current,conn)

    def seal(self,operation,target,candidate_digest,*,run_id=None):
        capability=self.state.get('manual_capability') if self.manual else None
        if self.manual:
            if (not isinstance(capability,dict) or capability.get('run_id')!=run_id
                    or target not in capability.get('targets',[]) or capability.get('operation_id')):
                raise CleanerError('manual_capability_lost','Ручной допуск не соответствует операции',409)
        if operation in self.state['seals']:raise CleanerError('dispatch_sealed','Допуск уже использован',409)
        self.state['seals'][operation]=dict(target=target,digest=candidate_digest)
        if self.manual:
            # Bind the newly minted durable operation to this one capability.
            # The seal remains forever; only the transient capability is cleared.
            self.state['manual_capability']=dict(capability,operation_id=operation,operation_candidate_digest=candidate_digest)
        self.guard._save(self.state)
        self._sealed_here.add(operation)
        # Not a transferable durable capability. Caller adds this only AFTER
        # the operational transaction committed successfully.

    def committed(self,operation):
        if not self.active or operation not in self._sealed_here:raise CleanerError('external_hold','Нет нового допуска этого процесса')
        self._sealed_here.remove(operation)
        self.rights.add(operation)

    def submit_once(self,operation,call):
        if not self.active or operation not in self.rights or process_identity()!=self.identity:
            raise CleanerError('dispatch_right_missing','Нет одноразового права отправки',409)
        self.rights.remove(operation) # consume even when the call raises/crashes
        return call()
