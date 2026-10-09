"""Query-only receipts from immutable native cleaner requests and results.

No service construction, migration, worker launch, admission or WB access. The
same operational read snapshot provides request, dated events and item proof.
"""
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import quote

DOMAIN='cleaner_operations'
LABEL='Чистка ключей: настройки и задания'
PREFIX='/v1/sheet-vitrina-v1/ads/keyword-cleaner'
TABLE="(SELECT 'cleaner-request:'||request_id operation_id,created_at accepted_at,* FROM cleaner_requests)"


@dataclass(frozen=True)
class CleanerScope:
    runtime_dir: Path
    account: str
    seller_id: str
    account_scope: str
    generation: str
    actor: str

    @classmethod
    def from_entrypoint(cls,entrypoint,*,actor):
        web=getattr(entrypoint,'cleaner_web',None)
        cleaner=getattr(web,'cleaner',None)
        if not cleaner or not actor:return None
        runtime=Path(entrypoint.runtime.runtime_dir).resolve()
        if Path(cleaner.store.registry.runtime_dir).resolve()!=runtime:
            raise ValueError('operator_cleaner_owner_binding_invalid')
        return cls(runtime,cleaner.key,cleaner.account.seller_id,cleaner.account.account_scope,
            web.generation,str(actor).strip().casefold())


def source(conn,*,selected,scope,db_path):
    if DOMAIN not in selected or not isinstance(scope,CleanerScope) or not scope.actor or not scope.generation:return None
    from packages.application.storage_registry import StoreRegistry
    from packages.contracts.search_cluster_cleaner import Account
    if Path(db_path).resolve()!=StoreRegistry(scope.runtime_dir).resolve('operational').resolve() or scope.account!=Account(scope.seller_id,scope.account_scope).key:
        raise ValueError('operator_cleaner_source_binding_invalid')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cleaner_requests'").fetchone():return None
    # Check the native runtime/account binding before counts, search or details.
    if not conn.execute('SELECT 1 FROM cleaner_settings WHERE account=? AND seller_id=? AND account_scope=? AND generation=?',
            (scope.account,scope.seller_id,scope.account_scope,scope.generation)).fetchone():return None
    return (TABLE,'operation_id','*','account=? AND actor=?',(scope.account,scope.actor),public)


def _facts(conn,account,kind,field,identity):
    row=conn.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind LIKE ? AND json_extract(facts,?)=? ORDER BY sequence DESC LIMIT 1",
        (account,kind,'$.'+field,identity)).fetchone()
    return json.loads(row[0]) if row else {}


def _write_proof(conn,account,run_id,*,target=None):
    if not run_id:return False,0
    run=conn.execute("SELECT kind,targets FROM cleaner_runs WHERE account=? AND run_id=?",(account,run_id)).fetchone()
    if not run or run['kind']!='manual_apply':return False,0
    frozen=json.loads(run['targets'])
    expected={(r['target'],r['query_hash'],r['decision_id']) for r in frozen}
    if not expected or len(expected)!=len(frozen):return False,len(frozen)
    if target is not None and {r[0] for r in expected}!={target}:return False,len(expected)
    actual=set();confirmed=True
    for op in conn.execute("SELECT operation_id,target,state,dispatch_count FROM cleaner_write_operations WHERE account=? AND run_id=? AND state<>'cancelled_before_send'",(account,run_id)):
        confirmed=confirmed and op['state']=='confirmed' and op['dispatch_count']==1
        readback=conn.execute("SELECT 1 FROM cleaner_events WHERE account=? AND run_id=? AND operation_id=? AND kind IN ('readback_result','late_confirmation') AND json_extract(facts,'$.state')='confirmed' AND json_extract(facts,'$.target')=? LIMIT 1",
            (account,run_id,op['operation_id'],op['target'])).fetchone()
        confirmed=confirmed and bool(readback)
        items=conn.execute('SELECT query_hash,decision_id,state,confirmed_at FROM cleaner_write_items WHERE operation_id=?',(op['operation_id'],)).fetchall()
        confirmed=confirmed and bool(items)
        for item in items:
            identity=(op['target'],item['query_hash'],item['decision_id'])
            if identity in actual:confirmed=False
            actual.add(identity)
            confirmed=confirmed and item['state']=='confirmed' and bool(item['confirmed_at'])
    return bool(confirmed and actual==expected),len(expected)


def _manual_result(conn,row,job_id,*,expected_target=None):
    initial=_facts(conn,row['account'],'self_service_requested','job_id',job_id)
    latest=_facts(conn,row['account'],'self_service_%','job_id',job_id)
    if not initial or initial.get('actor')!=row['actor'] or initial.get('account_key')!=row['account'] or (expected_target is not None and (initial.get('advert_id'),initial.get('nm_id'))!=expected_target):
        return dict(state='needs_attention',native_state='source_proof_missing',external_confirmed=False,
            reason_ru='Задание сохранено, но точная исходная привязка пока не подтверждена.')
    native=latest.get('state','queued');target=str(initial['advert_id'])+':'+str(initial['nm_id'])
    proof,count=_write_proof(conn,row['account'],latest.get('write_run_id'),target=target)
    scan=conn.execute("SELECT state FROM cleaner_runs WHERE account=? AND run_id=? AND kind='scan'",(row['account'],initial['scan_run_id'])).fetchone()
    scan_complete=bool(scan and scan['state']=='complete')
    if proof and native=='complete':state,reason='completed','Все изменения этого задания подтверждены сохранённой проверкой WB.'
    elif native=='no_change' and scan_complete and not latest.get('write_run_id'):state,reason='completed','Проверка завершена. Новых изменений WB не было.'
    elif native in {'complete','partial','failed','no_change'}:state,reason='needs_attention','Есть неподтверждённый или частичный результат. Повторная отправка не выполняется.'
    else:state,reason='processing','Задание сохранено. Его обычный исполнитель продолжает работу и проверку WB.'
    return dict(state=state,native_state=native,external_confirmed=proof and native=='complete',
        reason_ru=reason,expected_write_count=count,job_id=job_id,
        native_path=PREFIX+'/manual-clean/'+quote(job_id,safe=''))


def public(conn,row):
    outcome=json.loads(row['outcome']);route=row['route'];identity=row['operation_id']
    result=dict(contract_name='operator_operations_v1',operation_id=identity,domain=DOMAIN,title_ru=LABEL,
        durable_saved=True,accepted_at=row['accepted_at'],actor=row['actor'],state='completed',
        primary_effect='source_saved',native_state='source_saved',external_confirmed=False,calculation_completed=False,
        reason_ru='Бизнес-настройки сохранены. Работа WB этим документом не подтверждается.',
        resubmit_allowed=False,source_ref=dict(domain=DOMAIN,entity_id=row['request_id'],
            request_digest=row['digest'],account=row['account'],route=route),
        fields=[dict(label='Команда',value=route)],native_path=PREFIX+'/requests/'+quote(row['request_id'],safe=''))
    if route=='manual-clean' or route.startswith('manual-clean/') and route.endswith('/recheck'):
        result.update(_manual_result(conn,row,outcome.get('job_id')),primary_effect='native_job')
    elif route=='manual-batches' or route.startswith('manual-batches/') and route.endswith('/resume'):
        batch_id=outcome.get('batch_id') or row['request_id']
        initial=_facts(conn,row['account'],'self_service_batch_requested','batch_id',batch_id)
        latest=_facts(conn,row['account'],'self_service_batch_%','batch_id',batch_id)
        from packages.application.search_cluster_cleaner import batch_child_id
        children=[]
        for index,target in enumerate(initial.get('items') or []):
            child_id=batch_child_id(batch_id,index)
            child=_manual_result(conn,row,child_id,expected_target=(target['advert_id'],target['nm_id']))
            child.update(label_ru='Кампания '+str(target['advert_id'])+' · WB '+str(target['nm_id']))
            children.append(child)
        native=latest.get('state','queued')
        source_bound=initial.get('actor')==row['actor'] and initial.get('account_key')==row['account'] and bool(children)
        complete=bool(source_bound and native=='complete' and all(c['state']=='completed' for c in children))
        result.update(primary_effect='native_job',native_state=native,children=children,
            state='completed' if complete else 'needs_attention' if not source_bound or native in {'complete','partial','failed','attention_required'} else 'processing',
            external_confirmed=complete and all(c['external_confirmed'] for c in children),
            reason_ru='Проверка всех выбранных пар завершена. Результат каждой пары сохранён.' if complete else 'Сохранён результат исходной группы. Неподтверждённые пары показаны отдельно.',
            native_path=PREFIX+'/manual-batches/'+quote(batch_id,safe=''))
    elif outcome.get('run_id'):
        run=conn.execute('SELECT state,kind FROM cleaner_runs WHERE account=? AND run_id=?',(row['account'],outcome['run_id'])).fetchone()
        proof,count=_write_proof(conn,row['account'],outcome['run_id'])
        native=run['state'] if run else 'source_proof_missing'
        scan_done=bool(run and run['kind']=='scan' and native=='complete')
        result.update(primary_effect='native_job',native_state=native,external_confirmed=proof,
            state='completed' if proof or scan_done else 'needs_attention' if native in {'complete','failed','partial','stopped','source_proof_missing'} else 'processing',
            reason_ru='Проверка доступных ключей завершена; полнота охвата WB не подтверждается.' if scan_done else 'Результат изменения подтверждён.' if proof else 'Задание сохранено. Ожидается его обычная обработка и точная проверка результата.',
            expected_write_count=count,native_path=PREFIX+'/runs/'+quote(outcome['run_id'],safe=''))
    elif not (route in {'settings','daily-schedules'} or route.startswith('profiles/') or route.startswith('reviews/')):
        result.update(state='needs_attention',native_state='result_projection_unavailable',
            reason_ru='Команда сохранена. Этот вид пока не имеет общей проекции результата; проверьте исходную команду.')
    result['source_ref'].update({k:outcome[k] for k in ('settings_revision','revision','version','enabled','schedule_time') if k in outcome})
    result.update(journal_path='/sheet-vitrina-v1/operations?operation_id='+quote(identity,safe=''),
        detail_path='/v1/sheet-vitrina-v1/operations/'+quote(identity,safe=''))
    return result


def receipt(web,principal,request_id):
    """Decorate an existing native command response without changing its proof."""
    principal.require_read();cleaner=web.require_service()
    with cleaner.store.read() as conn:
        scope=CleanerScope(cleaner.store.registry.runtime_dir,cleaner.key,cleaner.account.seller_id,
            cleaner.account.account_scope,web.generation,principal.username.strip().casefold())
        spec=source(conn,selected={DOMAIN},scope=scope,db_path=cleaner.store.registry.resolve('operational'))
        if not spec:return None
        table,key,columns,where,values,reader=spec
        row=conn.execute(f'SELECT {columns} FROM {table} WHERE request_id=? AND ({where})',(request_id,*values)).fetchone()
        return reader(conn,row) if row else None
