"""Query-only common projection. Domain receipts remain authoritative."""
from contextlib import closing
from packages.application import operator_ff_overhead as overhead
from packages.application import operator_warehouse_documents as warehouse


def _overhead_public(conn,row):
    value=overhead._public(conn,row)
    value.update(kind='pool_overhead',document_kind='pool_overhead',title_ru='Накладные расходы ФФ',
                 physical_applied=bool(value['document']),domain='ff_pool_document',
                 journal_path='/sheet-vitrina-v1/vitrina?operation_id='+value['operation_id'])
    return value


def journal(db_path, *, page=1, limit=25):
    if isinstance(page,bool) or isinstance(limit,bool) or not 1<=page<=100000 or not 1<=limit<=100:
        raise ValueError('invalid_operation_journal_page')
    with closing(overhead.readonly(db_path)) as conn:
        sources=[]
        if overhead._exists(conn):
            sources.append((overhead.TABLE,_overhead_public))
        if warehouse.exists(conn):
            sources.append((warehouse.TABLE,warehouse._public))
        total=sum(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table,_ in sources)
        sql=' UNION ALL '.join(f"SELECT request_id,accepted_at,'{table}' source_table FROM {table}" for table,_ in sources)
        rows=conn.execute('SELECT * FROM ('+sql+') ORDER BY accepted_at DESC,request_id DESC LIMIT ? OFFSET ?',(limit,(page-1)*limit)).fetchall() if sources else []
        readers=dict(sources)
        items=[readers[row['source_table']](conn,conn.execute(f"SELECT * FROM {row['source_table']} WHERE request_id=?",(row['request_id'],)).fetchone()) for row in rows]
    return {'contract_name':'operator_operations_v1','status':'ready','items':items,'page':page,'limit':limit,'total':total,'has_more':page*limit<total}


def read_acceptance(db_path, identity):
    with closing(overhead.readonly(db_path)) as conn:
        canonical=overhead._resolve(conn,identity) if (overhead._exists(conn) or warehouse.exists(conn)) else identity
        for table,reader in ((overhead.TABLE,_overhead_public),(warehouse.TABLE,warehouse._public)):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():
                row=conn.execute(f'SELECT * FROM {table} WHERE request_id=?',(canonical,)).fetchone()
                if row:
                    return reader(conn,row)
    return None
