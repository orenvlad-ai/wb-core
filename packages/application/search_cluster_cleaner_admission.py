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

    @contextmanager
    def session(self,*,account,generation):
        with self._lock():
            state=self._load();self._previous_stopped(state)
            if state['hold'] or state['generation']!=generation or state['account']!=account.key:
                raise CleanerError('external_hold','Поколение не допущено',409)
            with self.store.read() as c:self._reconcile(state,c)
            identity=process_identity()
            if not identity:raise CleanerError('process_identity_unknown','Нет точной идентичности процесса')
            state['owner']=identity;self._save(state)
            session=AdmissionSession(self,state,identity)
            try:yield session
            finally:
                session.active=False
                current=self._load()
                if current.get('owner')==identity:
                    current['owner']=None;self._save(current)


class AdmissionSession:
    def __init__(self,guard,state,identity):
        self.guard,self.state,self.identity=guard,state,identity;self.active=True;self.rights=set();self._sealed_here=set()

    def check(self,conn,account,generation):
        current=self.guard._load()
        if (not self.active or process_identity()!=self.identity or current!=self.state
                or current['hold'] or current['account']!=account.key or current['generation']!=generation):
            raise CleanerError('external_hold','Процесс утратил внешний допуск',409)
        self.guard._reconcile(current,conn)

    def seal(self,operation,target,candidate_digest):
        if operation in self.state['seals']:raise CleanerError('dispatch_sealed','Допуск уже использован',409)
        self.state['seals'][operation]=dict(target=target,digest=candidate_digest)
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
