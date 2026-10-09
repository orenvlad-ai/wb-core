"""Query-only views of native feedback work. No submission or reconciliation.

Only application-owned, fixed source paths enter this surface. Buyer support
remains cabinet-bound; publication requires the saved native readback evidence.
"""
from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

DOMAINS = {
    'feedback_reply': ('Ответы на отзывы', '/v1/sheet-vitrina-v1/feedbacks/autoanswers/manual/generate'),
    'feedback_complaint': ('Жалобы на отзывы', '/v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected'),
    'buyer_support': ('Чаты и возвраты покупателей', '/v1/sheet-vitrina-v1/feedbacks/buyer-support/pilot/operation'),
}
DOMAIN_LABELS = {key: value[0] for key, value in DOMAINS.items()}
AI = 'operator_autoanswers.sheet_vitrina_v1_wb_'
AUTO_TABLE = '(SELECT \'feedback-reply:\'||processing_key operation_id,created_at accepted_at,* FROM ' + AI + 'autoanswer_jobs)'
BUYER_TABLE = '(SELECT \'buyer-support:\'||id operation_id,json_extract(data,\'$.started_at\') accepted_at,* FROM operator_buyer.operations)'


@dataclass(frozen=True)
class FeedbackSurface:
    runtime_dir: Path
    autoanswers_db: Path
    buyer_db: Path
    cabinet: str
    actor: str = ''
    account_id: str = ''

    @classmethod
    def from_entrypoint(cls, entrypoint, *, actor):
        runtime=Path(entrypoint.runtime.runtime_dir).resolve()
        if not getattr(entrypoint,'autoanswers_repository',None) or not getattr(entrypoint,'buyer_support_pilot',None):return None
        from packages.application.wb_autoanswers_runtime import autoanswers_store_path
        if Path(entrypoint.autoanswers_repository.db_path).resolve()!=autoanswers_store_path(runtime).resolve() or Path(entrypoint.buyer_support_pilot.store.path).resolve()!=runtime/'buyer-support'/'pilot.sqlite3':
            raise ValueError('operator_feedback_owner_binding_invalid')
        return cls(runtime, runtime/Path(entrypoint.autoanswers_repository.db_path).name,
                   runtime/'buyer-support'/'pilot.sqlite3',str(entrypoint.buyer_support_pilot.config.cabinet),actor,
                   str(getattr(getattr(entrypoint,'change_registry_read_surface',None),'seller_id','') or ''))


def _attach(conn, path, alias):
    # This is a fixed application source, never a browser-supplied path. Opening
    # it cannot initialize/migrate source storage or change a job on GET.
    if not path.exists():return False
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('operator_feedback_source_path_unsafe')
    conn.execute('ATTACH DATABASE ? AS ' + alias, (path.resolve().as_uri()+'?mode=ro',))
    return True


def _exists(conn, alias, table):
    return bool(conn.execute('SELECT 1 FROM '+alias+".sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())


def sources(conn, *, selected, scope):
    if not isinstance(scope, FeedbackSurface):return []
    from packages.application.wb_autoanswers_runtime import autoanswers_store_path
    runtime=scope.runtime_dir.resolve()
    if scope.autoanswers_db.resolve()!=autoanswers_store_path(runtime).resolve() or scope.buyer_db.resolve()!=runtime/'buyer-support'/'pilot.sqlite3':
        raise ValueError('operator_feedback_source_binding_invalid')
    result=[]
    if 'feedback_reply' in selected and _attach(conn,scope.autoanswers_db,'operator_autoanswers'):
        if _exists(conn,'operator_autoanswers','sheet_vitrina_v1_wb_autoanswer_jobs'):
            # Automatic worker attempts are not extra operator documents. The
            # source's user audit proves a manual/backlog/approval action.
            where="EXISTS(SELECT 1 FROM "+AI+"autoanswers_audit_events a WHERE a.aggregate_type='processing_job' AND a.aggregate_id=processing_key AND a.actor_type='user' AND a.actor_id=?)"
            if scope.actor:result.append((AUTO_TABLE,'operation_id','*',where,(scope.actor,),reply_public))
    if 'buyer_support' in selected and scope.cabinet and _attach(conn,scope.buyer_db,'operator_buyer'):
        if _exists(conn,'operator_buyer','operations'):
            if scope.actor:result.append((BUYER_TABLE,'operation_id','*',"cabinet=? AND json_extract(data,'$.actor')=?",(scope.cabinet,scope.actor),buyer_public))
    return result


def _base(identity, domain, date, actor, *, state, effect, reason, source, fields=()):
    return dict(contract_name='operator_operations_v1',operation_id=identity,domain=domain,
        title_ru=DOMAIN_LABELS[domain],accepted_at=date,actor=actor,durable_saved=True,
        state=state,primary_effect=effect,reason_ru=reason,source_ref=dict(domain=domain,**source),
        fields=list(fields),retry_owner='native_source',resubmit_allowed=False)


def reply_public(conn,row):
    from packages.application.wb_autoanswers_runtime import final_reply_hash
    job=dict(row)
    pub=conn.execute('SELECT * FROM '+AI+'publication_jobs WHERE processing_key=?',(job['processing_key'],)).fetchone()
    pub=dict(pub) if pub else None
    actor=conn.execute('SELECT actor_id FROM '+AI+"autoanswers_audit_events WHERE aggregate_type='processing_job' AND aggregate_id=? AND actor_type='user' ORDER BY created_at DESC,event_id DESC LIMIT 1",(job['processing_key'],)).fetchone()
    actor=(pub.get('requested_by') if pub else None) or job.get('manual_reviewed_by') or (actor['actor_id'] if actor else '')
    confirmed=False
    if pub:
        digest=pub.get('readback_hash')
        confirmed=bool(pub['state']=='published' and digest and digest==pub['normalized_reply_sha256']
            and final_reply_hash(pub.get('readback_answer') or '')==digest
            and pub['feedback_id']==job['feedback_id'] and pub['content_version']==job['content_version']
            and pub['content_version_hash']==job['content_version_hash']
            and conn.execute('SELECT 1 FROM '+AI+"autoanswers_audit_events WHERE aggregate_type='publication_job' AND aggregate_id=? AND event_type='publication_confirmed_by_readback' LIMIT 1",(pub['publication_key'],)).fetchone())
        if confirmed:state,reason='completed','Точный ответ подтверждён сохранённой проверкой WB.'
        elif pub['state'] in {'approved','publishing','publish_pending_readback','retryable_error'}:
            state,reason='processing','Публикация ожидает точной проверки WB. Повторная отправка не выполняется.'
        else:state,reason='needs_attention','Публикация не подтверждена. Проверьте исходное задание.'
        effect='external_command'
    else:
        if job['state'] in {'queued','processing','retryable_error'}:
            state,reason='processing','Задание сохранено. Подготовка ответа продолжается.'
        elif job['state'] in {'generated','needs_review'}:
            state,reason='completed','Черновик ответа сохранён. В WB он не опубликован.'
        else:state,reason='needs_attention','Подготовка ответа требует проверки исходного задания.'
        effect='reply_draft_saved' if job['state'] in {'generated','needs_review'} else 'native_job'
    result=_base(job['operation_id'],'feedback_reply',job['accepted_at'],actor,state=state,effect=effect,reason=reason,
        source=dict(entity_id=job['processing_key'],revision=job['content_version_hash'],
            feedback_id=job['feedback_id'],
            content_version=job['content_version'],manual_edit_revision=job['manual_edit_revision'],
            media_processing_version=job['media_processing_version'],publication_key=pub['publication_key'] if pub else ''),
        fields=[dict(label='Отзыв',value=job['feedback_id']),dict(label='Версия',value=job['content_version'])])
    result.update(external_confirmed=confirmed,native_state=pub['state'] if pub else job['state'],
        draft_saved=not pub and effect=='reply_draft_saved',readback_digest=pub.get('readback_hash') if pub else None,
        native_path='/v1/sheet-vitrina-v1/feedbacks/detail?feedback_id='+quote(job['feedback_id']))
    result['source_ref'].update(manual_reply_sha256=job.get('manual_reply_sha256') or '',
        manual_guard_passed=bool(job['manual_guard_passed']) if job.get('manual_guard_passed') is not None else None,
        publication_reply_sha256=pub['normalized_reply_sha256'] if pub else '')
    return result


def buyer_public(conn,row):
    op=json.loads(row['data'])
    readback=op.get('readback') or {}
    if op.get('kind')=='chat_send':
        proof=bool(readback.get('receipt_correlation') is True and len(readback.get('matching_event_ids') or [])==1)
    elif op.get('kind')=='claim_decision':
        expected=('2','5') if op.get('action')=='autorefund1' else ('2','10') if op.get('action')=='approve2' else None
        proof=bool(expected and readback.get('archive') is True and (readback.get('status'),readback.get('status_ex'))==expected)
    else:proof=False
    confirmed=bool(op.get('state')=='confirmed' and op.get('write_attempted') is True and proof)
    if confirmed:state,reason='completed','Действие подтверждено сохранённой проверкой WB.'
    elif op.get('state')=='pending_readback':state,reason='processing','Ожидается точная проверка WB.'
    else:state,reason='needs_attention','Результат внешней операции неизвестен. Допустимо только чтение той же операции.'
    result=_base(row['operation_id'],'buyer_support',row['accepted_at'],op.get('actor',''),
        state=state,effect='external_command',reason=reason,
        source=dict(entity_id=row['id'],request_id=op['request_id'],cabinet=row['cabinet'],
            revision=op['context_version'],attempt_id=op['attempt_id'],kind=op['kind'],claim_id=op.get('claim_id'),action=op.get('action')),
        fields=[dict(label='Чат / возврат',value=op['item_id']),dict(label='Действие',value=op['kind'])])
    result.update(external_confirmed=confirmed,native_state=op['state'],
        native_path='/v1/sheet-vitrina-v1/feedbacks/buyer-support/pilot/operation?operation_id='+quote(row['id']))
    return result


def complaint_items(*, selected, scope):
    if 'feedback_complaint' not in selected or not isinstance(scope,FeedbackSurface) or not scope.actor or not scope.account_id:return []
    # Read an existing owner's atomic JSON. Do not instantiate a job store: its
    # constructor can mark active jobs interrupted, which is not a projection.
    from packages.application.sheet_vitrina_v1_feedbacks_complaints import DEFAULT_SUBMIT_JOB_DIRNAME,_submit_request_digest
    path=scope.runtime_dir/DEFAULT_SUBMIT_JOB_DIRNAME/'jobs.json'
    if not path.exists():return []
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.stat().st_size>8*1024*1024:
        raise ValueError('operator_complaint_source_unreadable')
    payload=json.loads(path.read_bytes())
    jobs=payload.get('jobs')
    if not isinstance(jobs,list) or len(jobs)>5000 or any(not isinstance(job,dict) for job in jobs):
        raise ValueError('operator_complaint_source_inventory_invalid')
    items=[]
    for job in jobs:
        if job.get('requested_by')!=scope.actor or job.get('account_id')!=scope.account_id:continue
        # Native job success is a worker outcome. Actual per-row submission
        # proof remains in attempts/journal; no blanket external completion.
        identity=job.get('run_id');date=job.get('created_at')
        if not identity or not date:raise ValueError('operator_complaint_job_identity_invalid')
        selected_ids=job.get('selected_feedback_ids')
        source_payload=job.get('request_payload') or {}
        scope_proven=bool(isinstance(selected_ids,list) and selected_ids and len(selected_ids)==len(set(selected_ids))
            and job.get('selected_count')==len(selected_ids) and selected_ids==source_payload.get('feedback_ids')
            and scope.account_id==source_payload.get('account_id')
            and job.get('request_digest')==_submit_request_digest(source_payload,scope.actor))
        attempts={attempt.get('feedback_id'):attempt for attempt in job.get('attempts',[])}
        children=[]
        for target in selected_ids or []:
            attempt=attempts.get(target,{})
            proven=bool(attempt.get('attempt_status')=='submitted' and attempt.get('code')=='row_submit_confirmed_success'
                and attempt.get('run_id')==identity and target in job.get('submitted_feedback_ids',[]))
            children.append(dict(label_ru='Отзыв '+target,external_confirmed=proven,
                outcome='confirmed' if proven else 'ambiguous' if attempt.get('attempt_status')=='error' else 'created',
                native_state=attempt.get('attempt_status','missing'),attempt_id=identity+':'+target))
        confirmed=sum(child['external_confirmed'] for child in children)
        state='processing' if job['status'] in {'queued','running'} else 'needs_attention'
        reason='Задание жалоб сохранено. Итог каждой жалобы проверяется в исходном журнале.'
        if not scope_proven:state,reason='needs_attention','Нет точного сохранённого подтверждения выбранного набора. Проверьте исходный журнал жалоб.'
        elif confirmed==len(selected_ids):state,reason='completed','Подача каждой выбранной жалобы подтверждена native проверкой WB.'
        result=_base('feedback-complaint:'+identity,'feedback_complaint',date,job.get('requested_by',''),
            state=state,effect='external_command',reason=reason,
            source=dict(entity_id=identity,selected_feedback_ids=selected_ids or [],scope_proven=scope_proven,
                request_key=job.get('request_key',''),revision=job.get('request_digest',''),account_id=scope.account_id),
            fields=[dict(label='Выбрано',value=job.get('selected_count')),dict(label='Подано',value=job.get('submitted_count'))])
        result.update(native_state=job['status'],external_confirmed=bool(scope_proven and children and confirmed==len(children)),children=children,
            partial=0<confirmed<len(children),confirmed_count=confirmed,target_count=len(selected_ids or []),
            native_path='/v1/sheet-vitrina-v1/feedbacks/complaints/submit-job?run_id='+quote(identity))
        if not scope_proven:
            result['durable_saved']=False
            result['source_confirmation_missing']=True
        items.append(result)
    return items


def read_native(db_path, *, domain, native_id, allowed_domains, scope):
    if domain not in DOMAINS or not isinstance(native_id,str) or not native_id or len(native_id)>256:
        raise ValueError('operator_feedback_native_identity_invalid')
    if domain not in set(allowed_domains):return None
    from contextlib import closing
    from packages.application.operator_ff_overhead import readonly
    from packages.application.operator_operations import _common
    if domain=='feedback_complaint':
        found=[item for item in complaint_items(selected={domain},scope=scope) if item['source_ref']['request_key']==native_id]
        if len(found)>1:raise ValueError('operator_feedback_native_identity_ambiguous')
        return _common(found[0]) if found else None
    with closing(readonly(db_path)) as conn:
        specs=sources(conn,selected={domain},scope=scope)
        if not specs:return None
        table,key,columns,where,values,reader=specs[0]
        selector='processing_key=?' if domain=='feedback_reply' else "json_extract(data,'$.request_id')=?"
        lookup=(native_id,)
        if domain=='feedback_reply' and native_id.startswith('feedback-version:'):
            parts=native_id.split(':',2)
            if len(parts)!=3 or not parts[1].isdigit() or int(parts[1])<1 or not parts[2]:raise ValueError('operator_feedback_version_selector_invalid')
            selector='feedback_id=? AND content_version=?';lookup=(parts[2],int(parts[1]))
        found=conn.execute(f'SELECT {columns} FROM {table} WHERE {selector} AND ({where})',(*lookup,*values)).fetchall()
        if len(found)>1:raise ValueError('operator_feedback_native_identity_ambiguous')
        return _common(reader(conn,found[0])) if found else None


def decorate(result, *, db_path, scope, domain, native_id):
    result=dict(result)
    if not native_id:return result
    try:
        from packages.application.operator_operations import read_acceptance
        prefix={'feedback_reply':'feedback-reply:','feedback_complaint':'feedback-complaint:','buyer_support':'buyer-support:'}[domain]
        receipt=read_acceptance(db_path,prefix+native_id,allowed_domains={domain},feedback_scope=scope)
        if receipt:result['acceptance']=receipt
        else:result['operator_projection']={'status':'not_tracked','reason_code':'native_receipt_unavailable'}
    except (OSError,ValueError,sqlite3.Error):
        result['operator_projection']={'status':'not_tracked','reason_code':'native_receipt_unavailable'}
    return result
