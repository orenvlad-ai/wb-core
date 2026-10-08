"""Query-only common projection. Domain receipts remain authoritative."""
from contextlib import closing
from packages.application import operator_ff_overhead as overhead
from packages.application import operator_warehouse_documents as warehouse
from packages.application import operator_report_source_versions as report_sources
from packages.application import operator_fulfillment_services as fulfillment
from packages.application import operator_partner_report as partner_report
from packages.application import operator_supplier_journal as supplier_journal

DOMAIN_LABELS = {'ff_pool_document': 'Складские документы', fulfillment.DOMAIN: 'Услуги фулфилмента',
    'plan_report_baseline': 'Исходные данные отчётов',
    'factory_order_dataset': 'Исходные данные планирования',
    partner_report.DOMAIN: 'Настройки партнёрского отчёта'}
DOMAIN_LABELS.update(supplier_journal.LABELS)
DEFAULT_DOMAINS = frozenset({'ff_pool_document', fulfillment.DOMAIN})
DOMAIN_SECTIONS = {'ff_pool_document': 'supply', fulfillment.DOMAIN: 'supply',
    'plan_report_baseline': 'reports', 'factory_order_dataset': 'supply', partner_report.DOMAIN: 'reports'}
DOMAIN_SECTIONS.update({name: 'supply' for name in supplier_journal.LABELS})


def _allowed(allowed_domains, allowed_sections):
    if allowed_sections is None:
        return set(DEFAULT_DOMAINS if allowed_domains is None else allowed_domains).intersection(DOMAIN_LABELS)
    domains = {name for name, section in DOMAIN_SECTIONS.items() if section in set(allowed_sections)}
    return domains if allowed_domains is None else domains.intersection(allowed_domains)


def _common(value):
    value['journal_path'] = '/sheet-vitrina-v1/operations?operation_id=' + value['operation_id']
    value['detail_path'] = '/v1/sheet-vitrina-v1/operations/' + value['operation_id']
    return value


def _overhead_public(conn,row):
    value=overhead._public(conn,row)
    value.update(kind='pool_overhead',document_kind='pool_overhead',title_ru='Накладные расходы ФФ',
                 physical_applied=bool(value['document']),domain='ff_pool_document',
                 journal_path='/sheet-vitrina-v1/operations?operation_id='+value['operation_id'])
    return value


def journal(db_path, *, page=1, limit=25, allowed_domains=None, allowed_sections=None, domain='all', search='', request_scope='', supplier_safe=False, runtime_dir=None):
    if type(page) is not int or type(limit) is not int or not 1<=page<=100000 or not 1<=limit<=100:
        raise ValueError('invalid_operation_journal_page')
    allowed = _allowed(allowed_domains, allowed_sections)
    if domain not in ('', 'all', *DOMAIN_LABELS):
        raise ValueError('invalid_operation_domain')
    search = str(search or '').strip()
    if len(search) > 200:
        raise ValueError('invalid_operation_search')
    selected = allowed if domain in ('', 'all') else allowed.intersection({domain})
    with closing(overhead.readonly(db_path)) as conn:
        sources=supplier_journal.sources(conn, selected=selected, request_scope=request_scope, supplier_safe=supplier_safe, db_path=db_path, runtime_dir=runtime_dir)
        if 'ff_pool_document' in selected:
            if overhead._exists(conn):
                sources.append((overhead.TABLE, 'request_id', '*', '1', (), _overhead_public))
            if warehouse.exists(conn):
                sources.append((warehouse.TABLE, 'request_id', '*', '1', (), warehouse._public))
        if fulfillment.DOMAIN in selected and fulfillment._exists(conn, fulfillment.TABLE):
            sources.append((fulfillment.TABLE, 'operation_id', '*', '1', (), fulfillment._public))
        reports = tuple(sorted(selected.intersection(report_sources.DOMAINS)))
        if partner_report.DOMAIN in selected and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (partner_report.TABLE,)).fetchone():
            sources.append((partner_report.TABLE, 'operation_id', '*', '1', (), partner_report.public))
        if reports and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (report_sources.TABLE,)).fetchone():
            sources.append((report_sources.TABLE, 'operation_id', report_sources.PUBLIC_COLUMNS,
                'domain IN (' + ','.join('?' for _ in reports) + ')', reports,
                lambda connection, row: report_sources.public(dict(row))))
        if search:
            value = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            search_columns = {report_sources.TABLE: 'after_json', partner_report.TABLE: "product_name || ' ' || nm_id"}
            financial = supplier_journal.financial
            search_columns[financial.REQUESTS] = ("shipment_id || ' ' || action || ' ' || coalesce((SELECT group_concat(child.subject_id,' ') FROM "
                + financial.CHILDREN + ' child WHERE child.request_scope=' + financial.REQUESTS + '.request_scope AND child.request_id='
                + financial.REQUESTS + ".request_id),'')")
            if supplier_safe:
                # Safe supplier routes must not reveal internal operands through
                # search existence/counts even when rows themselves are redacted.
                search_columns[supplier_journal.shipments.TABLE] = (
                    "coalesce(json_extract(source_json,'$.header.invoice_no'),'') || ' ' || "
                    "coalesce(json_extract(source_json,'$.header.shipment_date'),'')")
            sources = [(table, key, columns, where + " AND lower(" + search_columns.get(table, 'source_json') + ") LIKE lower(?) ESCAPE '\\'",
                        (*params, value), reader) for table, key, columns, where, params, reader in sources]
        total = sum(conn.execute(f'SELECT count(*) FROM {table} WHERE {where}', params).fetchone()[0]
                    for table, _, _, where, params, _ in sources)
        # Distinct action families may share one native table. Keep the
        # exact scoped reader paired with its own UNION arm.
        sql = ' UNION ALL '.join(f"SELECT {identity} AS operation_id,accepted_at,'{index}' source_table FROM {table} WHERE {where}"
            for index, (table, identity, _, where, _, _) in enumerate(sources))
        params = tuple(value for _, _, _, _, values, _ in sources for value in values)
        rows = conn.execute('SELECT * FROM (' + sql + ') ORDER BY accepted_at DESC,operation_id DESC LIMIT ? OFFSET ?',
            (*params, limit, (page-1)*limit)).fetchall() if sources else []
        readers = {str(index): item for index, item in enumerate(sources)}
        items = []
        for row in rows:
            table, identity, columns, where, values, reader = readers[row['source_table']]
            native = conn.execute(f'SELECT {columns} FROM {table} WHERE {identity}=? AND ({where})',
                                  (row['operation_id'], *values)).fetchone()
            items.append(_common(reader(conn, native)))
    return {'contract_name':'operator_operations_v1','status':'ready','items':items,'page':page,'limit':limit,
        'total':total,'has_more':page*limit<total,
        'available_domains':[{'domain':key,'label_ru':DOMAIN_LABELS[key]} for key in sorted(allowed)]}


def read_acceptance(db_path, identity, *, allowed_domains=None, allowed_sections=None, domain='', request_scope='', supplier_safe=False, runtime_dir=None):
    allowed = _allowed(allowed_domains, allowed_sections)
    if domain:
        if domain not in DOMAIN_LABELS:
            raise ValueError('invalid_operation_domain')
        allowed.intersection_update({domain})
    with closing(overhead.readonly(db_path)) as conn:
        if request_scope and not supplier_safe and runtime_dir is not None:
            for family in sorted(allowed.intersection(supplier_journal.FINANCIAL_DOMAINS)):
                value = supplier_journal.financial_acceptance(conn, runtime_dir, db_path, identity,
                    request_scope=request_scope, domain=family)
                if value:
                    return _common(value)
        for table, key, columns, where, values, reader in supplier_journal.sources(conn,
                selected=allowed, request_scope=request_scope, supplier_safe=supplier_safe, db_path=db_path, runtime_dir=runtime_dir):
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})', (identity,*values)).fetchone()
            if row is not None:
                value=reader(conn,row)
                return _common(value) if value else None
        if fulfillment.DOMAIN in allowed and fulfillment._exists(conn, fulfillment.TABLE):
            row = conn.execute(f'SELECT * FROM {fulfillment.TABLE} WHERE operation_id=?', (identity,)).fetchone()
            if row:
                return _common(fulfillment._public(conn, row))
        if 'ff_pool_document' in allowed:
            canonical=overhead._resolve(conn,identity) if (overhead._exists(conn) or warehouse.exists(conn)) else identity
            for table,reader in ((overhead.TABLE,_overhead_public),(warehouse.TABLE,warehouse._public)):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():
                    row=conn.execute(f'SELECT * FROM {table} WHERE request_id=?',(canonical,)).fetchone()
                    if row:
                        return _common(reader(conn,row))
        reports = tuple(sorted(allowed.intersection(report_sources.DOMAINS)))
        if partner_report.DOMAIN in allowed and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (partner_report.TABLE,)).fetchone():
            row=conn.execute(f'SELECT * FROM {partner_report.TABLE} WHERE operation_id=?', (identity,)).fetchone()
            if row is not None:
                return _common(partner_report.public(conn,row))
        if reports and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (report_sources.TABLE,)).fetchone():
            row = conn.execute(f"SELECT {report_sources.PUBLIC_COLUMNS} FROM {report_sources.TABLE} WHERE operation_id=? AND domain IN ({','.join('?' for _ in reports)})",
                               (identity, *reports)).fetchone()
            if row:
                return _common(report_sources.public(dict(row)))
    return None
