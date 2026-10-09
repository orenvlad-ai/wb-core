"""TEST-only native Finance receipt projection; no bootstrap, writes or federation."""
from __future__ import annotations
import json
from urllib.parse import quote
from packages.application.finance_liquidity_cash import FinanceCashError

TABLE='finance_liquidity_operations'
SCOPE_WHERE="(scope='document.create' OR scope LIKE 'document.update:%' OR scope LIKE 'document.post:%' OR scope LIKE 'document.reverse:%' OR scope LIKE 'opening.replace:%' OR scope LIKE 'transfer.%' OR scope LIKE 'cash.reconciliation.%' OR scope='opening.common.prepare')"


def _where(actor,is_admin,search):
    if not actor:raise FinanceCashError('authentication_required','Identity required',401)
    where=SCOPE_WHERE;values=[]
    if not is_admin:where+=' AND actor=?';values.append(actor)
    if search:
        value='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        where+=" AND (operation_id LIKE ? ESCAPE '\\' OR scope LIKE ? ESCAPE '\\')"
        values.extend((value,value))
    return where,values


def _public(service,conn,row,*,store_id,permitted_balance):
    result=json.loads(row['result_json'])
    if result.get('operation_id')!=row['operation_id'] or not result.get('receipt_id'):
        raise FinanceCashError('finance_receipt_unavailable','Exact native receipt is unavailable',503)
    # Validate the existing native sealed ledger authority, never a copied digest
    # or the newest document state. The original immutable operation is retained.
    scope=row['scope'];draft=result.get('status')=='draft'
    action_required=result.get('action_required') or result.get('status')=='action_required'
    state='needs_attention' if action_required else 'completed'
    reason='Запрос сохранён. Требуется ваше решение; деньги пока не изменены.' if action_required else 'Операция сохранена в кассе TEST.'
    if draft:reason='Черновик сохранён. Деньги пока не изменены.'
    reconciliation_id=result.get('reconciliation_id')
    account_id=None
    native_state=result.get('status','committed')
    if reconciliation_id:
        account=conn.execute('SELECT account_id FROM finance_liquidity_cash_reconciliations WHERE reconciliation_id=?',(reconciliation_id,)).fetchone()
        if not account:raise FinanceCashError('finance_receipt_unavailable','Reconciliation source missing',503)
        account_id=account[0]
        reason='Сверка сохранена. Деньги по ней не изменены.'
        if account_id=='cash_vladislav' and not permitted_balance:
            reason='Сверка сохранена. Результат сверки скрыт.'
            native_state='hidden'
    return dict(contract_name='operator_operations_v1',domain='finance_cash_test',
        operation_id=row['operation_id'],accepted_at=row['created_at'],actor=row['actor'],
        title_ru='Касса TEST',state=state,durable_saved=True,
        primary_effect='draft' if draft else 'source_saved' if row['effect_root_document_id'] is None else 'cash_ledger',
        calculation_completed=False if row['effect_root_document_id'] is None else True,
        native_state=native_state,reason_ru=reason,
        source_ref=dict(domain='finance_cash_test',entity_id=row['operation_id'],revision=row['request_digest'],
            receipt_id=result['receipt_id'],scope=scope,store_id=store_id,store_mode='isolated_test',
            document_id=result.get('document_id'),reconciliation_id=reconciliation_id,
            account_id=account_id,effect_root_document_id=row['effect_root_document_id']),
        fields=[dict(label='Операция',value=scope),dict(label='Автор',value=row['actor'])],
        journal_path='/finance/?embedded=1#operator-journal',
        detail_path='/v1/finance/operator-operations/'+quote(row['operation_id'],safe=''),
        native_path='/v1/finance/operations/'+quote(row['operation_id'],safe=''),
        retry_owner='native_finance',resubmit_allowed=False)


def read(service,*,actor,is_admin,store_id,store_mode,permitted_balance=False,
         identity=None,page=1,limit=25,search=''):
    # The actual Finance sidecar selects store and authenticated actor. A browser
    # cannot pass a path, account, TEST flag or alternative principal.
    if store_mode!='isolated_test' or not store_id:
        raise FinanceCashError('finance_test_projection_unavailable','TEST source binding required',503)
    if type(page) is not int or not 1<=page<=100000 or type(limit) is not int or not 1<=limit<=100:
        raise FinanceCashError('invalid_request','Invalid page')
    if not isinstance(search,str) or len(search)>200:raise FinanceCashError('invalid_request','Invalid search')
    where,values=_where(actor,is_admin,search)
    with service._connect() as conn:
        if identity is not None:
            if not isinstance(identity,str) or not identity or len(identity)>256:raise FinanceCashError('invalid_request','Invalid identity')
            row=conn.execute('SELECT * FROM '+TABLE+' WHERE operation_id=? AND '+where,(identity,*values)).fetchone()
            if not row:raise FinanceCashError('operation_not_found','Operation not found',404)
            if row['effect_root_document_id'] is not None:service._assert_ledger_integrity(conn)
            return dict(contract_name='operator_operations_v1',operation=_public(service,conn,row,store_id=store_id,permitted_balance=permitted_balance))
        total=conn.execute('SELECT count(*) FROM '+TABLE+' WHERE '+where,values).fetchone()[0]
        rows=conn.execute('SELECT * FROM '+TABLE+' WHERE '+where+' ORDER BY created_at DESC,operation_id DESC LIMIT ? OFFSET ?',(*values,limit,(page-1)*limit)).fetchall()
        if any(row['effect_root_document_id'] is not None for row in rows):service._assert_ledger_integrity(conn)
        return dict(contract_name='operator_operations_v1',items=[_public(service,conn,r,store_id=store_id,permitted_balance=permitted_balance) for r in rows],
            total=total,page=page,limit=limit,has_more=page*limit<total,
            available_domains=[dict(domain='finance_cash_test',label_ru='Касса TEST')])
