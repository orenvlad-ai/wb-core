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
        detail.update(result_summary(cleaner,batch,detail))
        incomplete=detail.get('discovered_pairs',0)>detail.get('eligible_pairs',0)
        final='partial' if incomplete and batch['state']=='complete' else batch['state']
        reason='; '.join(part for part in (batch.get('error') or '',
            'Часть активных CPM-пар не допущена к проверке' if incomplete else '') if part)
        with cleaner.store.transaction() as c:
            c.execute("UPDATE cleaner_daily_occurrences SET state=?,checked_campaign_count=?,details=?,reason=?,updated_at=? WHERE account=? AND batch_id=? AND state='batch_queued'",
                      (final,len({item['advert_id'] for item in done}),json.dumps(detail,ensure_ascii=False),reason,cleaner.clock(),cleaner.key,pending['batch_id']))
        return dict(batch_id=pending['batch_id'],state=final)


def result_summary(cleaner, batch: dict, occurrence: dict | None=None) -> dict:
    """Read-only confirmed subtotals; an incomplete pair never erases others."""
    items=batch['items']
    run_ids=[item['write_run_id'] for item in items if item.get('write_run_id')]
    confirmations={}
    with cleaner.store.read() as c:
        rows=c.execute("""SELECT o.run_id,o.target,o.versions,i.query
            FROM cleaner_write_operations o JOIN cleaner_write_items i ON i.operation_id=o.operation_id
            WHERE o.account=? AND o.run_id IN (SELECT value FROM json_each(?))
              AND i.state='confirmed' AND i.confirmed_at IS NOT NULL""",
            (cleaner.key,json.dumps(run_ids))).fetchall() if run_ids else []
    for row in rows:
        versions=json.loads(row['versions'])
        retirement=versions.get('archive_retirement',{})
        counts=confirmations.setdefault((row['run_id'],row['target']),dict(excluded=set(),returned=set(),archived=set()))
        if row['query'] in versions.get('removals',[]):
            key='archived' if (retirement.get('policy')=='wb_archived_retirement_v1'
                              and row['query'] in retirement.get('queries',[])) else 'returned'
        else:key='excluded'
        counts[key].add(row['query'])
    excluded=[];returned=[];archived=[];deferred=[];unknown=0
    for item in items:
        # A finished scan with no write run and only missing statistics is
        # positive evidence of no write, not a missing receipt counted as zero.
        scan_only=(item.get('state')=='partial' and item.get('stage')=='finished'
                   and item.get('error_code')=='statistics_missing' and not item.get('write_run_id')
                   and item.get('checked_total') is not None and item.get('deferred_count',0)>0
                   and not item.get('can_recheck') and not item.get('review_required'))
        ex=0 if scan_only else item.get('confirmed_excluded')
        ret=0 if scan_only else item.get('returned')
        if item.get('write_run_id'):
            # All three subtotals come from the same confirmed-item snapshot,
            # including partially confirmed operations. Never subtract a newer
            # archive count from an older batch projection's return count.
            counts=confirmations.get((item['write_run_id'],f"{item['advert_id']}:{item['nm_id']}"),{})
            excluded.append(len(counts.get('excluded',())))
            returned.append(len(counts.get('returned',())))
            archived.append(len(counts.get('archived',())))
        else:
            if type(ex) is int:excluded.append(ex)
            if type(ret) is int:returned.append(ret);archived.append(0)
        if type(item.get('deferred_count')) is int:deferred.append(item['deferred_count'])
        unknown+=bool(ex is None or ret is None or item.get('can_recheck') or item.get('review_required')
                      or item.get('write_run_id') and item.get('delivery_state') not in {'confirmed','not_sent'})
    occurrence=occurrence or {}
    unknown+=max(0,occurrence.get('discovered_pairs',0)-occurrence.get('eligible_pairs',0))
    return dict(report_version=1,summary_available=True,
                confirmed_excluded=sum(excluded) if excluded else None,
                archived_retired=sum(archived) if returned else None,
                confirmed_returned=sum(returned) if returned else None,
                deferred_count=sum(deferred) if deferred else None,
                failed_count=batch['failed_count'],held_count=batch['held_count'],unknown_pairs=unknown,
                started_at=batch.get('created_at'),
                finished_at=batch.get('updated_at') if batch['state'] in {'complete','partial','failed'} else None)


def historical_summary(cleaner, batch_id: str, occurrence: dict) -> dict:
    """Read a compact legacy receipt, without projecting every key/decision.

    Requests and latest child events retain the same account, owner and frozen
    target checks as the full public batch view. Unknown children remain unknown.
    """
    from packages.application.search_cluster_cleaner import batch_child_id
    with cleaner.store.read() as c:
        c.execute('BEGIN')
        try:
            request=c.execute("SELECT actor FROM cleaner_requests WHERE account=? AND request_id=? AND route='manual-batches'",
                              (cleaner.key,batch_id)).fetchone()
            if not request:return {}
            actor=Principal(request['actor'],True,True,True)
            saved=cleaner.manual_batch_snapshot(batch_id,actor,_conn=c)
            ids=[batch_child_id(batch_id,index) for index in range(len(saved['items']))]
            requests={r['request_id']:json.loads(r['outcome']) for r in c.execute(
                """SELECT request_id,outcome FROM cleaner_requests WHERE account=? AND actor=?
                   AND route='manual-clean' AND request_id IN (SELECT value FROM json_each(?))""",
                (cleaner.key,request['actor'],json.dumps(ids)))}
            events={}
            for row in c.execute("""SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_%'
                    AND json_extract(facts,'$.job_id') IN (SELECT value FROM json_each(?)) ORDER BY sequence""",
                    (cleaner.key,json.dumps(ids))):
                facts=json.loads(row['facts']);events[facts['job_id']]=facts
            jobs={}
            def matches(value,index,frozen):
                return (value.get('batch_id')==batch_id and value.get('batch_index')==index
                        and value.get('advert_id')==frozen['advert_id'] and value.get('nm_id')==frozen['nm_id'])
            for index,frozen in enumerate(saved['items']):
                job_id=ids[index];initial=requests.get(job_id)
                if initial is None:continue
                latest=events.get(job_id,{})
                job=dict(initial,**latest)
                job.update(state=latest.get('state','queued'),stage=latest.get('stage','fetching'))
                if not matches(initial,index,frozen) or not matches(job,index,frozen):
                    jobs[index]=None  # A collision is an explicit failure, never a receipt.
                else:jobs[index]=job
            run_ids=sorted({run_id for job in jobs.values() if job for run_id in
                            (job.get('scan_run_id'),job.get('write_run_id')) if run_id})
            runs={r['run_id']:dict(r) for r in c.execute(
                "SELECT run_id,state,kind,reason,summary,targets FROM cleaner_runs WHERE account=? AND run_id IN (SELECT value FROM json_each(?))",
                (cleaner.key,json.dumps(run_ids)))}
            operations={}
            for row in c.execute("""SELECT o.run_id,o.target,o.state,
                    json_group_array(json_object('query_hash',i.query_hash,'decision_id',i.decision_id)) AS refs,
                    sum(CASE WHEN i.state NOT IN ('confirmed','rejected') OR
                              (i.state='confirmed' AND i.confirmed_at IS NULL) THEN 1 ELSE 0 END) AS unresolved
                    FROM cleaner_write_operations o LEFT JOIN cleaner_write_items i ON i.operation_id=o.operation_id
                    WHERE o.account=? AND o.run_id IN (SELECT value FROM json_each(?))
                    GROUP BY o.operation_id""",(cleaner.key,json.dumps(run_ids))):
                operations.setdefault((row['run_id'],row['target']),[]).append(dict(row))
            items=[]
            for index,frozen in enumerate(saved['items']):
                update=saved['item_updates'].get(str(index),{});job=jobs.get(index)
                collision=index in jobs and job is None
                state='failed' if collision else update.get('state') or (job['state'] if job else 'queued')
                item=dict(advert_id=frozen['advert_id'],nm_id=frozen['nm_id'],state=state,
                    stage='finished' if collision else update.get('stage') or (job['stage'] if job else 'queued'),
                    error_code='batch_child_collision' if collision else update.get('error_code') or (job.get('error_code') if job else None),
                    review_required=bool(update.get('review_required')),
                    can_recheck=bool(job and job.get('can_recheck')),
                    write_run_id=job.get('write_run_id') if job else None,
                    checked_total=None,deferred_count=None,confirmed_excluded=None,returned=None,delivery_state='unknown')
                if job:
                    scan=runs.get(job.get('scan_run_id'))
                    if scan and scan['state']=='complete':
                        counts=json.loads(scan['summary'])
                        item.update(checked_total=counts.get('checked_total'),deferred_count=counts.get('excluded_not_executed'))
                    write_id=job.get('write_run_id')
                    if write_id:
                        run=runs.get(write_id)
                        # A missing/foreign run cannot supply confirmed zeroes.
                        if not run:raise CleanerError('not_found','Запуск не найден',404)
                        item.update(confirmed_excluded=0,returned=0)
                        ops=operations.get((write_id,f"{frozen['advert_id']}:{frozen['nm_id']}"),[])
                        active=[op for op in ops if op['state']!='cancelled_before_send']
                        expected={(r.get('target'),r.get('query_hash'),r.get('decision_id')) for r in json.loads(run['targets'])}
                        actual={(op['target'],r['query_hash'],r['decision_id']) for op in active for r in json.loads(op['refs'])}
                        if (expected and expected==actual and active
                                and all(op['state']=='confirmed' and not op['unresolved'] for op in active)):
                            item['delivery_state']='confirmed'
                        elif not active and (any(op['state']=='cancelled_before_send' for op in ops)
                                             or run['state']=='stopped' and run['reason']=='batch_write_wait'):
                            item['delivery_state']='not_sent'
                    elif job['state'] in {'complete','no_change'}:
                        item.update(confirmed_excluded=0,returned=0)
                items.append(item)
            not_started=lambda item:item['stage']=='not_started' or item['error_code']=='not_started_after_failure'
            batch=dict(items=items,state=saved['state'],created_at=saved['created_at'],updated_at=saved['updated_at'],
                       failed_count=sum(i['state']=='failed' and not i['review_required'] and not not_started(i) for i in items),
                       held_count=sum(i['review_required'] for i in items))
        finally:c.rollback()
    return result_summary(cleaner,batch,occurrence)


def history(cleaner,principal:Principal,*,limit:int=30) -> dict:
    principal.require_read()
    if not schema_ready(cleaner):return dict(items=[],timezone='Asia/Yekaterinburg',available=False)
    with cleaner.store.read() as c:
        rows=c.execute('SELECT * FROM cleaner_daily_occurrences WHERE account=? ORDER BY due_at DESC LIMIT ?',
                       (cleaner.key,min(max(limit,1),100))).fetchall()
        queued=[row['batch_id'] for row in rows if row['state']=='batch_queued']
        batch_states={}
        if queued:
            for event in c.execute("""SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE 'self_service_batch_%'
                    AND json_extract(facts,'$.batch_id') IN (SELECT value FROM json_each(?)) ORDER BY sequence""",
                    (cleaner.key,json.dumps(queued))):
                facts=json.loads(event['facts']);batch_states[facts['batch_id']]=facts.get('state','queued')
    result=[]
    for row in rows:
        item=dict(schedule_id=row['schedule_id'],local_date=row['local_date'],due_at=row['due_at'],
                  state=batch_states.get(row['batch_id'],'queued') if row['state']=='batch_queued' else row['state'],
                  batch_id=row['batch_id'],discovered_campaigns=row['discovered_count'],
                  details=json.loads(row['details']),reason=row['reason'],checked_campaigns=row['checked_campaign_count'],
                  checked_keys=None,excluded=None,returned=None)
        item['checked_keys']=item['details'].get('checked_keys')
        item['excluded']=item['details'].get('excluded')
        item['returned']=item['details'].get('returned')
        summary=item['details'] if item['details'].get('report_version')==1 else {}
        if (not summary or summary.get('unknown_pairs',0)>0) and row['state'] in {'complete','partial','failed'} and row['batch_id']:
            try:summary=historical_summary(cleaner,row['batch_id'],item['details'])
            except CleanerError:summary={}
        for name in ('confirmed_excluded','archived_retired','confirmed_returned','deferred_count',
                     'failed_count','held_count','unknown_pairs','started_at','finished_at'):
            item[name]=summary.get(name)
        item['summary_available']=summary.get('summary_available',False)
        result.append(item)
    return dict(items=result,timezone='Asia/Yekaterinburg')
