"""Owner-managed daily slots over the guarded, single-worker batch queue."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from packages.application.business_data_procedure_admission import business_write_is_blocked
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.contracts.search_cluster_cleaner import CleanerError, Principal, digest

ZONE = ZoneInfo('Asia/Yekaterinburg')
TIME_RE = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
POLICY_FILENAME = '.auto-updates-policy.json'
MAX_SLOTS = 12
RETRY_MINUTES = (0, 15, 30, 60, 120)
RETRY_GRACE = timedelta(minutes=1)


def retry_window(due: datetime, now: datetime) -> tuple[int | None, int | None]:
    """Return the current window and next window; neither means exhausted."""
    for index, minutes in enumerate(RETRY_MINUTES):
        scheduled=due+timedelta(minutes=minutes)
        if now<scheduled:return None,index
        if now<scheduled+RETRY_GRACE:
            return index,index+1 if index+1<len(RETRY_MINUTES) else None
    return None,None


def retry_details(due: datetime, index: int | None, next_index: int | None, prior: dict | None=None) -> dict:
    result=dict(prior or {})
    result['last_attempt_index']=index if index is not None else (next_index-1 if next_index is not None else len(RETRY_MINUTES)-1)
    result['next_retry_at']=(due+timedelta(minutes=RETRY_MINUTES[next_index])).isoformat() if next_index is not None else None
    return result


def schema_ready(cleaner) -> bool:
    with cleaner.store.read() as c:
        version=c.execute('SELECT version FROM cleaner_schema WHERE singleton=1').fetchone()
        return bool(version and version[0]>=3 and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cleaner_daily_schedules'").fetchone())


def policy_ready(runtime_dir: Path) -> tuple[bool, str]:
    try:
        policy = json.loads((runtime_dir / POLICY_FILENAME).read_text(encoding='utf-8'))
        if not isinstance(policy, dict) or type(policy.get('master_desired')) is not bool:
            return False, 'Общая политика автообновлений не подтверждена'
        master_desired=policy['master_desired']
    except (OSError, ValueError):
        return False, 'Общая политика автообновлений недоступна'
    maintenance_path=runtime_dir / '.business-data-maintenance.json'
    if maintenance_path.exists():
        try:
            maintenance=json.loads(maintenance_path.read_text(encoding='utf-8'))
            if not isinstance(maintenance,dict):raise ValueError('invalid maintenance')
            phase=maintenance.get('phase')
            if phase in {'holding','prepared','held','restoring'}:
                return False, 'Техническое обслуживание приостановило расписание'
            if phase!='restored':return False, 'Состояние обслуживания не подтверждено'
        except (OSError,ValueError):
            return False, 'Состояние обслуживания не подтверждено'
    if business_write_is_blocked(runtime_dir):
        return False, 'Техническое обслуживание блокирует новые записи'
    if not master_desired:return False, 'Общая пауза автообновлений'
    return True, ''


def schedules(cleaner, principal: Principal, generation: str) -> dict:
    principal.require_read()
    available=schema_ready(cleaner)
    with cleaner.store.read() as c:
        settings=cleaner._settings(c)
        rows=c.execute('SELECT schedule_id,local_time,enabled,owner,actor_authority,generation FROM cleaner_daily_schedules WHERE account=? ORDER BY local_time,schedule_id',(cleaner.key,)).fetchall() if available else []
    ready,reason=policy_ready(cleaner.store.registry.runtime_dir)
    return dict(revision=settings['revision'],timezone='Asia/Yekaterinburg',
                effective=available and ready and bool(settings['baseline_ready']) and not settings['enabled'],
                pause_reason='Ожидается обновление схемы расписания' if not available else reason or ('Исходная база не подтверждена' if not settings['baseline_ready'] else ''),
                schedules=[dict(id=r['schedule_id'],time=r['local_time'],enabled=bool(r['enabled']),owner=r['owner']) for r in rows] if rows else [dict(id='default',time='03:45',enabled=False,owner='')],
                can_edit=available and settings['generation']==generation and bool(cleaner.owner_username.strip()) and
                    (principal.site_owner or principal.username.strip().casefold()==cleaner.owner_username.strip().casefold()))


def save_schedules(cleaner, principal: Principal, generation: str, payload: dict) -> dict:
    principal.require_owner(cleaner.owner_username)
    requested=payload.get('schedules')
    if not isinstance(requested,list) or len(requested)>MAX_SLOTS:
        raise CleanerError('schedule_invalid','Допустимо не более 12 времён',422)
    seen=set();times=set();normalized=[]
    for item in requested:
        if not isinstance(item,dict) or set(item)!={'id','time','enabled'}:
            raise CleanerError('schedule_invalid','Некорректная строка расписания',422)
        sid,t,enabled=item['id'],item['time'],item['enabled']
        if (not isinstance(sid,str) or not ID_RE.fullmatch(sid) or not isinstance(t,str)
                or not TIME_RE.fullmatch(t) or type(enabled) is not bool or sid in seen or t in times):
            raise CleanerError('schedule_invalid','Повтор или неверное время расписания',422)
        seen.add(sid);times.add(t);normalized.append((sid,t,int(enabled)))
    def command(c,actor):
        version=c.execute('SELECT version FROM cleaner_schema WHERE singleton=1').fetchone()
        if not version or version[0]<3:
            raise CleanerError('schedule_schema_required','Расписание ожидает обновления схемы',503)
        settings=cleaner._settings(c)
        if settings['generation']!=generation or settings['revision']!=payload.get('expected_revision'):
            raise CleanerError('revision_conflict','Расписание уже изменилось',409)
        if settings['enabled']:
            raise CleanerError('legacy_scheduler_active','Сначала остановите прежний режим',409)
        if any(enabled for _,_,enabled in normalized) and not settings['baseline_ready']:
            raise CleanerError('schedule_not_ready','Исходная база ещё не подтверждена',409)
        previous={r['schedule_id']:r for r in c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=?',(cleaner.key,))}
        authority='bootstrap_operator' if principal.site_owner else 'configured_owner'
        changed_at=cleaner.clock()
        c.execute('DELETE FROM cleaner_daily_schedules WHERE account=?',(cleaner.key,))
        for sid,t,enabled in normalized:
            old=previous.get(sid)
            unchanged=bool(old and old['local_time']==t and old['enabled']==enabled and old['owner']==actor
                           and old['actor_authority']==authority and old['generation']==generation)
            c.execute('INSERT INTO cleaner_daily_schedules VALUES(?,?,?,?,?,?,?,?)',
                      (cleaner.key,sid,t,enabled,actor,authority,generation,old['created_at'] if unchanged else changed_at))
        c.execute('UPDATE cleaner_settings SET revision=revision+1 WHERE account=?',(cleaner.key,))
        cleaner._event(c,'daily_schedule_saved',dict(actor=actor,timezone='Asia/Yekaterinburg',schedules=normalized))
        return dict(revision=settings['revision']+1,schedules=[dict(id=sid,time=t,enabled=bool(enabled)) for sid,t,enabled in normalized])
    return cleaner._command(principal,'daily-schedules',payload,command)


def occurrence_id(cleaner, sid: str, local_date: str) -> str:
    return 'daily-'+digest([cleaner.key,sid,local_date])[:48]


class DailyCleanerScheduler:
    def __init__(self, cleaner, *, generation: str, source_factory=None, fixture_admission=None, now=None,
                 bootstrap_owner_username: str='', deployment_check=None):
        self.cleaner=cleaner
        self.generation=generation
        self.source_factory=source_factory
        self.fixture_admission=fixture_admission
        self.now=now or (lambda: datetime.now(timezone.utc))
        self.bootstrap_owner_username=bootstrap_owner_username.strip().casefold()
        self.deployment_check=deployment_check

    @staticmethod
    def _binding(row) -> dict:
        return dict(owner=row['owner'],authority=row['actor_authority'],generation=row['generation'],
                    slot_created_at=row['created_at'])

    def _bound(self,item,schedule) -> bool:
        if not schedule or not schedule['enabled'] or not self._principal(schedule):return False
        details=json.loads(item['details'])
        return all(details.get(key)==value for key,value in self._binding(schedule).items())

    def _deployment_ready(self) -> bool:
        # The real WB source requires an explicit release check. A synthetic
        # source may omit it for isolated fixture tests.
        return bool(self.deployment_check()) if self.deployment_check is not None else self.source_factory is not None

    def _principal(self,row):
        actor=row['owner']
        if row['actor_authority']=='configured_owner' and actor==self.cleaner.owner_username.strip().casefold():
            return Principal(actor,True,True,True)
        if row['actor_authority']=='bootstrap_operator' and actor==self.bootstrap_owner_username and actor:
            return Principal(actor,True,True,True,site_owner=True)
        return None

    def _ready(self) -> bool:
        with self.cleaner.store.read() as c:
            s=self.cleaner._settings(c)
            return s['generation']==self.generation and not s['enabled'] and s['baseline_ready']

    def _cancel_prestart_for_master_pause(self) -> int:
        """Close unstarted intents on an explicit pause, including retry waits."""
        cleaner=self.cleaner
        states="('pending','catalog_wait','start_wait','deployment_blocked')"
        unqueued="""NOT EXISTS (SELECT 1 FROM cleaner_requests r
            WHERE r.account=o.account AND r.request_id=o.batch_id AND r.route='manual-batches')"""
        with cleaner.store.read() as c:
            candidate=c.execute(f'''SELECT 1 FROM cleaner_daily_occurrences o
                WHERE o.account=? AND o.state IN {states} AND {unqueued} LIMIT 1''',(cleaner.key,)).fetchone()
        if not candidate:return 0
        with cleaner.store.transaction() as c:
            # Maintenance takes precedence over the master pause: diagnostic
            # writes must not enter its guarded window.
            if policy_ready(cleaner.store.registry.runtime_dir)[1]!='Общая пауза автообновлений':return 0
            return c.execute(f'''UPDATE cleaner_daily_occurrences AS o
                SET state='skipped',reason='Общая пауза автообновлений',updated_at=?
                WHERE o.account=? AND o.state IN {states} AND {unqueued}''',
                (cleaner.clock(),cleaner.key)).rowcount

    def observe_deployment_blocked(self) -> int:
        """Record due slots without catalog reads or queue admission before release."""
        cleaner=self.cleaner
        if not schema_ready(cleaner):return 0
        policy,reason=policy_ready(cleaner.store.registry.runtime_dir)
        if not policy:
            if reason=='Общая пауза автообновлений':
                self._cancel_prestart_for_master_pause()
            return 0
        if not self._ready():return 0
        now=self.now().astimezone(timezone.utc)
        local=now.astimezone(ZONE)
        today=local.date().isoformat()
        with cleaner.store.read() as c:
            rows=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND enabled=1 ORDER BY local_time,schedule_id',(cleaner.key,)).fetchall()
            existing={r['schedule_id']:r for r in c.execute('SELECT * FROM cleaner_daily_occurrences WHERE account=? AND local_date=?',(cleaner.key,today))}
            older=c.execute("SELECT * FROM cleaner_daily_occurrences WHERE account=? AND state='deployment_blocked' AND local_date<? ORDER BY due_at LIMIT 48",(cleaner.key,today)).fetchall()
        for item in older:self._advance_blocked(item,now)
        inserted=0
        for row in rows:
            prior=existing.get(row['schedule_id'])
            if prior:
                if prior['state']=='deployment_blocked':self._advance_blocked(prior,now)
                continue
            if not self._principal(row) or row['generation']!=self.generation:continue
            hour,minute=map(int,row['local_time'].split(':'))
            due=local.replace(hour=hour,minute=minute,second=0,microsecond=0).astimezone(timezone.utc)
            if now<due:continue
            index,next_index=retry_window(due,now)
            exhausted=next_index is None and (index is None or index==len(RETRY_MINUTES)-1)
            details=retry_details(due,index,next_index)
            if not policy_ready(cleaner.store.registry.runtime_dir)[0]:return inserted
            with cleaner.store.transaction() as c:
                current=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',(cleaner.key,row['schedule_id'])).fetchone()
                if (not current or not current['enabled'] or not self._principal(current)
                        or current['generation']!=self.generation or current['local_time']!=row['local_time']
                        or due<datetime.fromisoformat(current['created_at'].replace('Z','+00:00'))):continue
                details.update(owner=current['owner'],authority=current['actor_authority'],
                               generation=current['generation'],slot_created_at=current['created_at'])
                state='not_started' if exhausted else 'deployment_blocked'
                message='Последнее окно запуска прошло при незавершённом выпуске' if exhausted else 'Выпуск сервиса не завершён'
                inserted+=c.execute('''INSERT OR IGNORE INTO cleaner_daily_occurrences
                    (account,schedule_id,local_date,due_at,state,batch_id,details,reason,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (cleaner.key,row['schedule_id'],today,due.isoformat(),state,
                     occurrence_id(cleaner,row['schedule_id'],today),json.dumps(details,ensure_ascii=False),message,
                     cleaner.clock(),cleaner.clock())).rowcount
        return inserted

    def _advance_blocked(self,item,now:datetime) -> None:
        due=datetime.fromisoformat(item['due_at'].replace('Z','+00:00'))
        index,next_index=retry_window(due,now)
        exhausted=next_index is None and (index is None or index==len(RETRY_MINUTES)-1)
        old=json.loads(item['details'])
        details=retry_details(due,index,next_index,old)
        blocked_reason='Выпуск сервиса не завершён'
        with self.cleaner.store.read() as c:
            schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                               (self.cleaner.key,item['schedule_id'])).fetchone()
        if self._bound(item,schedule) and not exhausted and details==old and item['reason']==blocked_reason:return
        if not policy_ready(self.cleaner.store.registry.runtime_dir)[0]:return
        with self.cleaner.store.transaction() as c:
            latest=c.execute("SELECT state FROM cleaner_daily_occurrences WHERE account=? AND schedule_id=? AND local_date=?",
                             (self.cleaner.key,item['schedule_id'],item['local_date'])).fetchone()
            if not latest or latest['state']!='deployment_blocked':return
            schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                               (self.cleaner.key,item['schedule_id'])).fetchone()
            if (not schedule or not schedule['enabled'] or not self._principal(schedule)
                or schedule['owner']!=old.get('owner') or schedule['actor_authority']!=old.get('authority')
                or schedule['generation']!=old.get('generation') or schedule['created_at']!=old.get('slot_created_at')):
                state,message='skipped','Расписание выключено или изменено до завершения выпуска'
            else:
                state='not_started' if exhausted else 'deployment_blocked'
                message='Последнее окно запуска прошло при незавершённом выпуске' if exhausted else blocked_reason
            c.execute("UPDATE cleaner_daily_occurrences SET state=?,details=?,reason=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state='deployment_blocked'",
                      (state,json.dumps(details,ensure_ascii=False),message,self.cleaner.clock(),self.cleaner.key,item['schedule_id'],item['local_date']))

    def recover_deployment_blocked(self,now:datetime,*,allowed:bool,reason:str) -> None:
        """Classify blocked intents before the ordinary guarded queue cycle."""
        cleaner=self.cleaner
        with cleaner.store.read() as c:
            rows=c.execute("SELECT * FROM cleaner_daily_occurrences WHERE account=? AND state='deployment_blocked' ORDER BY due_at,schedule_id LIMIT 48",(cleaner.key,)).fetchall()
        for item in rows:
            due=datetime.fromisoformat(item['due_at'].replace('Z','+00:00'))
            bound=json.loads(item['details'])
            def classify(schedule,request):
                if request:return 'batch_queued','',bound
                if (not schedule or not schedule['enabled'] or not self._principal(schedule)
                    or schedule['generation']!=self.generation
                    or schedule['local_time']!=due.astimezone(ZONE).strftime('%H:%M')
                    or schedule['owner']!=bound.get('owner') or schedule['actor_authority']!=bound.get('authority')
                    or schedule['generation']!=bound.get('generation')
                    or schedule['created_at']!=bound.get('slot_created_at')):
                    return 'skipped','Расписание выключено или изменено до завершения выпуска',bound
                if not allowed:return 'skipped',reason or 'Расписание больше не допущено',bound
                index,next_index=retry_window(due,now)
                if next_index is None and index is None:
                    return 'not_started','Окна запуска завершились после незавершённого выпуска',retry_details(due,index,next_index,bound)
                if index is None or index<=bound.get('last_attempt_index',-1):
                    updated=retry_details(due,index,next_index,bound)
                    message='Ожидает повторной попытки; ранее выпуск сервиса не был завершён'
                    return ('deployment_blocked',message,updated) if updated!=bound or item['reason']!=message else None
                updated=retry_details(due,index,next_index,bound)
                updated['next_retry_at']=None  # Durable queue admission has no retry deadline.
                return 'pending','',updated
            with cleaner.store.read() as c:
                schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',(cleaner.key,item['schedule_id'])).fetchone()
                request=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",(cleaner.key,item['batch_id'])).fetchone()
            if classify(schedule,request) is None:continue
            with cleaner.store.transaction() as c:
                latest=c.execute("SELECT state FROM cleaner_daily_occurrences WHERE account=? AND schedule_id=? AND local_date=?",
                                 (cleaner.key,item['schedule_id'],item['local_date'])).fetchone()
                if not latest or latest['state']!='deployment_blocked':continue
                schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                                   (cleaner.key,item['schedule_id'])).fetchone()
                request=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",
                                  (cleaner.key,item['batch_id'])).fetchone()
                action=classify(schedule,request)
                if action is None:continue
                state,message,details=action
                c.execute("UPDATE cleaner_daily_occurrences SET state=?,details=?,reason=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state='deployment_blocked'",
                          (state,json.dumps(details,ensure_ascii=False),message,cleaner.clock(),cleaner.key,item['schedule_id'],item['local_date']))

    def resume_old_start_wait(self,now:datetime,today:str) -> None:
        """Retry a previous-day pre-start failure without creating another intent."""
        cleaner=self.cleaner
        with cleaner.store.read() as c:
            rows=c.execute("SELECT * FROM cleaner_daily_occurrences WHERE account=? AND local_date<? AND state IN ('start_wait','catalog_wait') ORDER BY due_at LIMIT 48",
                           (cleaner.key,today)).fetchall()
        for item in rows:
            due=datetime.fromisoformat(item['due_at'].replace('Z','+00:00'))
            index,next_index=retry_window(due,now)
            old=json.loads(item['details'])
            details=retry_details(due,index,next_index,old)
            with cleaner.store.read() as c:
                schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                                   (cleaner.key,item['schedule_id'])).fetchone()
            if not self._bound(item,schedule):state,message='skipped','Расписание изменилось до повторной попытки'
            elif index is None and next_index is None:state,message='not_started','Все пять окон запуска прошли: '+(item['reason'] or 'нет допуска новой группы')
            elif index is not None and index>old.get('last_attempt_index',-1):
                state,message='pending','';details['next_retry_at']=None
            elif details!=old:state,message='start_wait',item['reason']
            else:continue
            with cleaner.store.transaction() as c:
                c.execute("UPDATE cleaner_daily_occurrences SET state=?,reason=?,details=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state IN ('start_wait','catalog_wait')",
                          (state,message,json.dumps(details,ensure_ascii=False),cleaner.clock(),cleaner.key,item['schedule_id'],item['local_date']))

    def tick(self) -> dict | None:
        cleaner=self.cleaner
        if not schema_ready(cleaner):return None
        now=self.now().astimezone(timezone.utc)
        local=now.astimezone(ZONE)
        today=local.date().isoformat()
        local_ready=self._ready()
        ready,reason=policy_ready(cleaner.store.registry.runtime_dir)
        # Maintenance and an unreadable policy forbid even diagnostic writes.
        if not ready and reason!='Общая пауза автообновлений':return None
        self.reconcile_pending(today)
        if reason=='Общая пауза автообновлений':self._cancel_prestart_for_master_pause()
        with cleaner.store.read() as c:
            rows=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND enabled=1 ORDER BY local_time,schedule_id',(cleaner.key,)).fetchall()
            existing_today={r['schedule_id']:r for r in c.execute('SELECT * FROM cleaner_daily_occurrences WHERE account=? AND local_date=?',(cleaner.key,today))}
        self.recover_deployment_blocked(now,allowed=local_ready and ready,reason=reason)
        if local_ready and ready:self.resume_old_start_wait(now,today)
        # Record every due slot before asking WB or the shared queue. A
        # pre-start failure waits for a fixed window; pending queue work does not.
        admitted_result=None
        for row in rows:
            if not self._principal(row) or row['generation']!=self.generation:continue
            hour,minute=map(int,row['local_time'].split(':'))
            due=local.replace(hour=hour,minute=minute,second=0,microsecond=0).astimezone(timezone.utc)
            if now<due:continue
            sid=row['schedule_id'];batch_id=occurrence_id(cleaner,sid,today)
            prior=existing_today.get(sid)
            if prior and prior['state'] not in {'catalog_wait','start_wait'}:continue
            index,next_index=retry_window(due,now)
            old_details=json.loads(prior['details']) if prior else {}
            if prior and index is not None and index<=old_details.get('last_attempt_index',-1):continue
            details=retry_details(due,index,next_index,old_details)
            if prior and index is None and details==old_details:continue
            with cleaner.store.transaction() as c:
                current=c.execute('SELECT enabled,owner,actor_authority,generation,local_time,created_at FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',(cleaner.key,sid)).fetchone()
                if (not current or not current['enabled'] or not self._principal(current)
                        or current['generation']!=self.generation or current['local_time']!=row['local_time']):continue
                if due<datetime.fromisoformat(current['created_at'].replace('Z','+00:00')):continue
                details.update(self._binding(current))
                item=c.execute('SELECT * FROM cleaner_daily_occurrences WHERE account=? AND schedule_id=? AND local_date=?',(cleaner.key,sid,today)).fetchone()
                if item and item['state'] not in {'catalog_wait','start_wait'}:continue
                existing=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",(cleaner.key,batch_id)).fetchone()
                if existing:
                    if item:
                        c.execute("UPDATE cleaner_daily_occurrences SET state='batch_queued',updated_at=? WHERE account=? AND schedule_id=? AND local_date=?",
                                  (cleaner.clock(),cleaner.key,sid,today))
                    else:
                        c.execute('''INSERT OR IGNORE INTO cleaner_daily_occurrences
                            (account,schedule_id,local_date,due_at,state,batch_id,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?)''',(cleaner.key,sid,today,due.isoformat(),'batch_queued',batch_id,cleaner.clock(),cleaner.clock()))
                    continue
                if not local_ready or not ready:
                    state,message='skipped',reason or 'Исходная база или поколение не подтверждены'
                elif index is None and next_index is None:
                    state,message='not_started','Все пять окон запуска прошли до допуска новой группы'
                elif index is None:
                    state,message='start_wait','Ожидаем следующее окно запуска'
                else:
                    state,message='pending',''
                    details['next_retry_at']=None
                if item:
                    c.execute("UPDATE cleaner_daily_occurrences SET state=?,reason=?,details=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state IN ('catalog_wait','start_wait')",
                              (state,message,json.dumps(details,ensure_ascii=False),cleaner.clock(),cleaner.key,sid,today))
                else:
                    c.execute('''INSERT OR IGNORE INTO cleaner_daily_occurrences
                        (account,schedule_id,local_date,due_at,state,batch_id,details,reason,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''',
                        (cleaner.key,sid,today,due.isoformat(),state,batch_id,json.dumps(details,ensure_ascii=False),message,cleaner.clock(),cleaner.clock()))
                if state in {'skipped','not_started'}:admitted_result=admitted_result or dict(batch_id=batch_id,state=state)
        with cleaner.store.read() as c:
            item=c.execute("SELECT * FROM cleaner_daily_occurrences WHERE account=? AND state='pending' ORDER BY due_at,schedule_id LIMIT 1",
                           (cleaner.key,)).fetchone()
        if item is None:return admitted_result
        if not local_ready or not ready:
            self._finish(item,'skipped',reason or 'Расписание больше не допущено')
            return dict(batch_id=item['batch_id'],state='skipped')
        # One queue serves manual and scheduled batches. Busy work leaves all
        # admitted occurrences pending; none can create a duplicate batch.
        if self._busy():return self._mark_queue_wait(item)
        try:return self._enqueue(item)
        except CleanerError as exc:
            if exc.code in {'manual_batch_active','manual_job_active','manual_queue_blocked'}:
                return self._mark_queue_wait(item)
            if exc.code in {'schedule_changed','schedule_authority_changed'}:
                self._finish(item,'skipped',str(exc))
                return dict(batch_id=item['batch_id'],state='skipped')
            if exc.code=='deployment_blocked':
                if json.loads(item['details']).get('queue_admitted'):
                    return dict(batch_id=item['batch_id'],state='waiting_for_queue',reason=str(exc))
                return self._defer_start(item,now,str(exc),deployment_blocked=True)
            return self._defer_start(item,now,str(exc))
        except Exception:
            return self._defer_start(item,now,'Не удалось получить текущий каталог WB')

    def reconcile_pending(self,today:str) -> None:
        """Close disabled intents while preserving a slot admitted on time."""
        cleaner=self.cleaner
        with cleaner.store.read() as c:
            rows=c.execute("SELECT * FROM cleaner_daily_occurrences WHERE account=? AND state IN ('pending','catalog_wait','start_wait') ORDER BY due_at LIMIT 24",(cleaner.key,)).fetchall()
        for item in rows:
            with cleaner.store.read() as c:
                request=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",(cleaner.key,item['batch_id'])).fetchone()
                schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                                   (cleaner.key,item['schedule_id'])).fetchone()
            if not request and schedule and schedule['generation']==self.generation and self._bound(item,schedule):continue
            with cleaner.store.transaction() as c:
                request=c.execute("SELECT 1 FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",(cleaner.key,item['batch_id'])).fetchone()
                schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                                   (cleaner.key,item['schedule_id'])).fetchone()
                state=None;reason=''
                if request:state='batch_queued'
                elif not schedule or schedule['generation']!=self.generation or not self._bound(item,schedule):
                    state='skipped';reason='Расписание выключено или владелец изменился'
                if state:
                    c.execute("UPDATE cleaner_daily_occurrences SET state=?,reason=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state IN ('pending','catalog_wait','start_wait')",
                              (state,reason,cleaner.clock(),cleaner.key,item['schedule_id'],item['local_date']))

    def _busy(self) -> bool:
        from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
        # The durable admission inside start_manual_batch is the final lock.
        with self.cleaner.store.read() as c:
            if c.execute("SELECT 1 FROM cleaner_runs WHERE account=? AND state IN ('queued','accepted','running') LIMIT 1",(self.cleaner.key,)).fetchone():return True
        return bool(BatchCleanerCoordinator(self.cleaner,generation=self.generation,fixture_admission=self.fixture_admission).pending_batches())

    def _mark_queue_wait(self,item) -> dict:
        if not json.loads(item['details']).get('queue_admitted'):
            with self.cleaner.store.transaction() as c:
                latest=c.execute("SELECT details FROM cleaner_daily_occurrences WHERE account=? AND schedule_id=? AND local_date=? AND state='pending'",
                                 (self.cleaner.key,item['schedule_id'],item['local_date'])).fetchone()
                if latest:
                    details=json.loads(latest['details']);details['queue_admitted']=True
                    c.execute("UPDATE cleaner_daily_occurrences SET details=?,reason=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state='pending'",
                              (json.dumps(details,ensure_ascii=False),'Ожидает освобождения общей очереди',self.cleaner.clock(),
                               self.cleaner.key,item['schedule_id'],item['local_date']))
        return dict(batch_id=item['batch_id'],state='waiting_for_queue')

    def _defer_start(self,item,now:datetime,reason:str,*,deployment_blocked:bool=False) -> dict:
        due=datetime.fromisoformat(item['due_at'].replace('Z','+00:00'))
        index,next_index=retry_window(due,now)
        exhausted=next_index is None and (index is None or index==len(RETRY_MINUTES)-1)
        state='not_started' if exhausted else 'deployment_blocked' if deployment_blocked else 'start_wait'
        message=('Последнее окно запуска завершилось: ' if exhausted else '')+reason
        with self.cleaner.store.transaction() as c:
            latest=c.execute('SELECT details FROM cleaner_daily_occurrences WHERE account=? AND schedule_id=? AND local_date=? AND state=?',
                             (self.cleaner.key,item['schedule_id'],item['local_date'],'pending')).fetchone()
            if latest:
                details=retry_details(due,index,next_index,json.loads(latest['details']))
                c.execute("UPDATE cleaner_daily_occurrences SET state=?,reason=?,details=?,updated_at=? WHERE account=? AND schedule_id=? AND local_date=? AND state='pending'",
                          (state,message,json.dumps(details,ensure_ascii=False),self.cleaner.clock(),self.cleaner.key,item['schedule_id'],item['local_date']))
        return dict(batch_id=item['batch_id'],state=state,reason=message)

    def _finish(self,item,state,reason='',*,discovered=None,details=None):
        with self.cleaner.store.transaction() as c:
            latest=c.execute('SELECT details FROM cleaner_daily_occurrences WHERE account=? AND schedule_id=? AND local_date=?',
                             (self.cleaner.key,item['schedule_id'],item['local_date'])).fetchone()
            merged=json.loads(latest['details']) if latest else {}
            merged.update(details or {})
            if state=='batch_queued':merged['next_retry_at']=None
            c.execute('''UPDATE cleaner_daily_occurrences SET state=?,reason=?,discovered_count=?,details=?,updated_at=?
                WHERE account=? AND schedule_id=? AND local_date=? AND state IN ('pending','catalog_wait','start_wait')''',
                      (state,reason,discovered,json.dumps(merged,ensure_ascii=False),self.cleaner.clock(),
                       self.cleaner.key,item['schedule_id'],item['local_date']))

    def _enqueue(self,item):
        cleaner=self.cleaner
        if not self._deployment_ready():raise CleanerError('deployment_blocked','Выпуск сервиса не завершён',503)
        with cleaner.store.read() as c:
            schedule=c.execute('SELECT * FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                               (cleaner.key,item['schedule_id'])).fetchone()
        if not self._bound(item,schedule):raise CleanerError('schedule_changed','Расписание изменилось до запуска',409)
        from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
        source=self.source_factory() if self.source_factory else CleanerWbSource.from_env(cleaner.account)
        targets,errors,statuses=source.catalog(with_statuses=True)
        if any(not (error.startswith('adverts_missing:') and error.removeprefix('adverts_missing:').isdigit()
                    and statuses.get(int(error.removeprefix('adverts_missing:'))) in {7,-1}) for error in errors):
            raise CleanerError('campaign_catalog_incomplete','Каталог WB вернул неполные данные',409)
        active=[t for t in targets if t.payment_type=='cpm' and t.status==9]
        rows=eligibility_rows(cleaner,self.generation,active,fixture_admission=self.fixture_admission)
        selected=[r for r in rows if r['eligible'] and r['status']=='active']
        discovered=len({t.advert_id for t in active})
        if not selected:
            self._finish(item,'no_targets','Нет допущенных активных CPM-пар',discovered=discovered,
                         details=dict(discovered_pairs=len(active),eligible_pairs=0))
            return dict(batch_id=item['batch_id'],state='no_targets')
        if not self._deployment_ready():raise CleanerError('deployment_blocked','Выпуск сервиса не завершён',503)
        with cleaner.store.read() as c:
            schedule=c.execute('SELECT owner,actor_authority FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',(cleaner.key,item['schedule_id'])).fetchone()
        actor=self._principal(schedule) if schedule else None
        if actor is None:raise CleanerError('schedule_authority_changed','Владелец расписания изменился',409)
        payload=dict(request_id=item['batch_id'],selected_categories=['active'],
                     targets=[dict(advert_id=r['advert_id'],nm_id=r['nm_id']) for r in selected])
        ready,reason=policy_ready(cleaner.store.registry.runtime_dir)
        if not ready or not self._ready():
            self._finish(item,'skipped',reason or 'Расписание больше не допущено')
            return dict(batch_id=item['batch_id'],state='skipped')
        result=cleaner.start_manual_batch(payload,actor,snapshot=rows,
                                          daily_occurrence=(item['schedule_id'],item['local_date']),
                                          daily_deployment_check=self.deployment_check)
        self._finish(item,'batch_queued',discovered=discovered,
                     details=dict(discovered_pairs=len(active),eligible_pairs=len(selected),selected_campaigns=len({r['advert_id'] for r in selected})))
        return result

    def reconcile_finished(self) -> dict | None:
        """Persist one compact terminal receipt; expensive batch detail is read once."""
        from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator,batch_status
        cleaner=self.cleaner
        if not schema_ready(cleaner):return None
        with cleaner.store.read() as c:
            pending=c.execute("SELECT * FROM cleaner_daily_occurrences WHERE account=? AND state='batch_queued' ORDER BY due_at LIMIT 1",(cleaner.key,)).fetchone()
            if pending is None:return None
            latest=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%' AND json_extract(facts,'$.batch_id')=? ORDER BY sequence DESC LIMIT 1",
                             (cleaner.key,pending['batch_id'])).fetchone()
        if not latest or json.loads(latest['facts']).get('state') not in {'complete','partial','failed'}:return None
        coordinator=BatchCleanerCoordinator(cleaner,generation=self.generation,bootstrap_owner_username=self.bootstrap_owner_username)
        actor=coordinator._actor_for_batch(pending['batch_id'])
        batch=batch_status(cleaner,pending['batch_id'],actor)
        items=batch['items']
        done=[item for item in items if item['checked_total'] is not None]
        detail=json.loads(pending['details'])
        detail.update(checked_keys=sum(item['checked_total'] for item in done) if len(done)==len(items) else None,
                      excluded=sum(item['confirmed_excluded'] for item in items) if all(item['confirmed_excluded'] is not None for item in items) else None,
                      returned=sum(item['returned'] for item in items) if all(item['returned'] is not None for item in items) else None,
                      batch_state=batch['state'],selected_count=len(items),partial_count=batch['partial_count'],
                      failed_count=batch['failed_count'],skipped_count=batch['skipped_count'],
                      error=batch.get('error'),error_code=batch.get('error_code'))
        incomplete=detail.get('discovered_pairs',0)>detail.get('eligible_pairs',0)
        final='partial' if incomplete and batch['state']=='complete' else batch['state']
        reason='; '.join(part for part in (batch.get('error') or '',
            'Часть активных CPM-пар не допущена к проверке' if incomplete else '') if part)
        with cleaner.store.transaction() as c:
            c.execute("UPDATE cleaner_daily_occurrences SET state=?,checked_campaign_count=?,details=?,reason=?,updated_at=? WHERE account=? AND batch_id=? AND state='batch_queued'",
                      (final,len({item['advert_id'] for item in done}),json.dumps(detail,ensure_ascii=False),reason,cleaner.clock(),cleaner.key,pending['batch_id']))
        return dict(batch_id=pending['batch_id'],state=final)


def history(cleaner,principal:Principal,*,limit:int=30) -> dict:
    principal.require_read()
    if not schema_ready(cleaner):return dict(items=[],timezone='Asia/Yekaterinburg',available=False)
    with cleaner.store.read() as c:
        rows=c.execute('''SELECT o.*, (SELECT json_extract(e.facts,'$.state') FROM cleaner_events e
            WHERE e.account=o.account AND e.kind LIKE 'self_service_batch_%'
              AND json_extract(e.facts,'$.batch_id')=o.batch_id ORDER BY e.sequence DESC LIMIT 1) AS batch_state
            FROM cleaner_daily_occurrences o WHERE o.account=? ORDER BY o.due_at DESC LIMIT ?''',
                       (cleaner.key,min(max(limit,1),100))).fetchall()
    result=[]
    for row in rows:
        item=dict(schedule_id=row['schedule_id'],local_date=row['local_date'],due_at=row['due_at'],
                  state=(row['batch_state'] or 'queued') if row['state']=='batch_queued' else row['state'],
                  batch_id=row['batch_id'],discovered_campaigns=row['discovered_count'],
                  details=json.loads(row['details']),reason=row['reason'],checked_campaigns=row['checked_campaign_count'],
                  checked_keys=None,excluded=None,returned=None)
        item['checked_keys']=item['details'].get('checked_keys')
        item['excluded']=item['details'].get('excluded')
        item['returned']=item['details'].get('returned')
        result.append(item)
    return dict(items=result,timezone='Asia/Yekaterinburg')
