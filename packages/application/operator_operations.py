"""Query-only common projection. Domain receipts remain authoritative."""
from contextlib import closing
from packages.application import operator_ff_overhead as overhead
from packages.application import operator_warehouse_documents as warehouse
from packages.application import operator_report_source_versions as report_sources

DOMAIN_LABELS = {'ff_pool_document': 'Складские документы',
    'plan_report_baseline': 'Исходные данные отчётов',
    'factory_order_dataset': 'Исходные данные планирования'}
DEFAULT_DOMAINS = frozenset({'ff_pool_document'})


def _common(value):
    value['journal_path'] = '/sheet-vitrina-v1/operations?operation_id=' + value['operation_id']
    value['detail_path'] = '/v1/sheet-vitrina-v1/operations/' + value['operation_id']
    return value


def _overhead_public(conn,row):
    value=overhead._public(conn,row)
    value.update(kind='pool_overhead',document_kind='pool_overhead',title_ru='Накладные расходы ФФ',
                 physical_applied=bool(value['document']),domain='ff_pool_document',
                 journal_path='/sheet-vitrina-v1/vitrina?operation_id='+value['operation_id'])
    return value


def journal(db_path, *, page=1, limit=25, allowed_domains=DEFAULT_DOMAINS, domain='all'):
    if isinstance(page,bool) or isinstance(limit,bool) or not 1<=page<=100000 or not 1<=limit<=100:
        raise ValueError('invalid_operation_journal_page')
    allowed = set(allowed_domains).intersection(DOMAIN_LABELS)
    selected = allowed if domain == 'all' else allowed.intersection({domain})
    with closing(overhead.readonly(db_path)) as conn:
        sources=[]
        if 'ff_pool_document' in selected:
            if overhead._exists(conn):
                sources.append((overhead.TABLE, 'request_id', '*', '1', (), _overhead_public))
            if warehouse.exists(conn):
                sources.append((warehouse.TABLE, 'request_id', '*', '1', (), warehouse._public))
        reports = tuple(sorted(selected.intersection(report_sources.DOMAINS)))
        if reports and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (report_sources.TABLE,)).fetchone():
            sources.append((report_sources.TABLE, 'operation_id', report_sources.PUBLIC_COLUMNS,
                'domain IN (' + ','.join('?' for _ in reports) + ')', reports,
                lambda connection, row: report_sources.public(dict(row))))
        total = sum(conn.execute(f'SELECT count(*) FROM {table} WHERE {where}', params).fetchone()[0]
                    for table, _, _, where, params, _ in sources)
        sql = ' UNION ALL '.join(f"SELECT {identity} AS operation_id,accepted_at,'{table}' source_table FROM {table} WHERE {where}"
            for table, identity, _, where, _, _ in sources)
        params = tuple(value for _, _, _, _, values, _ in sources for value in values)
        rows = conn.execute('SELECT * FROM (' + sql + ') ORDER BY accepted_at DESC,operation_id DESC LIMIT ? OFFSET ?',
            (*params, limit, (page-1)*limit)).fetchall() if sources else []
        readers = {item[0]: item for item in sources}
        items = []
        for row in rows:
            table, identity, columns, where, values, reader = readers[row['source_table']]
            native = conn.execute(f'SELECT {columns} FROM {table} WHERE {identity}=? AND ({where})',
                                  (row['operation_id'], *values)).fetchone()
            items.append(_common(reader(conn, native)))
    return {'contract_name':'operator_operations_v1','status':'ready','items':items,'page':page,'limit':limit,
        'total':total,'has_more':page*limit<total,
        'available_domains':[{'domain':key,'label_ru':DOMAIN_LABELS[key]} for key in sorted(allowed)]}


def read_acceptance(db_path, identity, *, allowed_domains=DEFAULT_DOMAINS):
    allowed = set(allowed_domains).intersection(DOMAIN_LABELS)
    with closing(overhead.readonly(db_path)) as conn:
        if 'ff_pool_document' in allowed:
            canonical=overhead._resolve(conn,identity) if (overhead._exists(conn) or warehouse.exists(conn)) else identity
            for table,reader in ((overhead.TABLE,_overhead_public),(warehouse.TABLE,warehouse._public)):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():
                    row=conn.execute(f'SELECT * FROM {table} WHERE request_id=?',(canonical,)).fetchone()
                    if row:
                        return _common(reader(conn,row))
        reports = tuple(sorted(allowed.intersection(report_sources.DOMAINS)))
        if reports and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (report_sources.TABLE,)).fetchone():
            row = conn.execute(f"SELECT {report_sources.PUBLIC_COLUMNS} FROM {report_sources.TABLE} WHERE operation_id=? AND domain IN ({','.join('?' for _ in reports)})",
                               (identity, *reports)).fetchone()
            if row:
                return _common(report_sources.public(dict(row)))
    return None
