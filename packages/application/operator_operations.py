"""Query-only common projection. Domain receipts remain authoritative."""
from contextlib import closing
from packages.application import operator_ff_overhead as overhead
from packages.application import operator_warehouse_documents as warehouse
from packages.application import operator_report_source_versions as report_sources
from packages.application import operator_fulfillment_services as fulfillment
from packages.application import operator_partner_report as partner_report
from packages.application import operator_supplier_journal as supplier_journal
from packages.application import operator_trade_documents as trade
from packages.application import operator_supplier_contracts as contracts
from packages.application import operator_facility_mappings as facilities
from packages.application import operator_compat_uploads as compat_uploads
from packages.application import operator_nomenclature as nomenclature
from packages.application import operator_external_operations as external
from packages.application import operator_feedback_operations as feedback
from packages.application import operator_business_settings as business_settings
from packages.application import operator_autoanswers_settings as ai_settings
from packages.application import operator_feedback_analysis_settings as analysis_settings
from packages.application import operator_feedback_complaint_schedules as complaint_schedules
from packages.application import operator_complaint_runs as complaint_runs
from packages.application import operator_cleaner_operations as cleaner
from packages.application import operator_balance_jobs as balance_jobs
from packages.application import operator_spp_jobs as spp_jobs

DOMAIN_LABELS = {nomenclature.DOMAIN: 'Справочник SKU', 'registry_bundle_upload': 'Справочники через API', 'cost_price_upload': 'Себестоимость через API', 'ff_pool_document': 'Складские документы', fulfillment.DOMAIN: 'Услуги фулфилмента',
    'plan_report_baseline': 'Исходные данные отчётов',
    'factory_order_dataset': 'Исходные данные планирования',
    partner_report.DOMAIN: 'Настройки партнёрского отчёта'}
DOMAIN_LABELS.update(supplier_journal.LABELS)
DOMAIN_LABELS.update(external.DOMAIN_LABELS)
DOMAIN_LABELS.update(feedback.DOMAIN_LABELS)
DOMAIN_LABELS[ai_settings.DOMAIN]=ai_settings.LABEL
DOMAIN_LABELS[analysis_settings.DOMAIN]=analysis_settings.LABEL
DOMAIN_LABELS[complaint_schedules.DOMAIN]=complaint_schedules.LABEL
DOMAIN_LABELS[complaint_runs.DOMAIN]="Ручные запуски авто-жалоб"
DOMAIN_LABELS[trade.DOMAIN] = 'Библиотека инвойсов и договоров'
DOMAIN_LABELS[contracts.DOMAIN]='Договоры поставщика'
DOMAIN_LABELS[facilities.DOMAIN] = "Склады и связи FBS"
DOMAIN_LABELS[business_settings.DOMAIN]='Бизнес-настройки'
DOMAIN_LABELS[cleaner.DOMAIN]=cleaner.LABEL
DOMAIN_LABELS[balance_jobs.DOMAIN]=balance_jobs.LABEL
DOMAIN_LABELS[spp_jobs.DOMAIN]=spp_jobs.LABEL
DEFAULT_DOMAINS = frozenset({'ff_pool_document', fulfillment.DOMAIN})
DOMAIN_SECTIONS = {nomenclature.DOMAIN: 'settings', 'ff_pool_document': 'supply', fulfillment.DOMAIN: 'supply',
    'plan_report_baseline': 'reports', 'factory_order_dataset': 'supply', partner_report.DOMAIN: 'reports'}
DOMAIN_SECTIONS.update({name: 'supply' for name in supplier_journal.LABELS})
DOMAIN_SECTIONS[trade.DOMAIN] = 'settings'
DOMAIN_SECTIONS[contracts.DOMAIN] = 'supply'
DOMAIN_SECTIONS[facilities.DOMAIN] = 'supply'


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


def _trade_sources(conn, *, selected, request_scope, supplier_safe):
    if supplier_safe or not request_scope or trade.DOMAIN not in selected or not trade.exists(conn, trade.TABLE):
        return []
    actions = tuple(sorted(trade.ACTIONS))
    return [(trade.TABLE, 'operation_id', '*',
        'request_scope=? AND action IN (' + ','.join('?' for _ in actions) + ')',
        (request_scope, *actions), lambda connection, row: trade._public(row)['acceptance'])]


CONTRACT_SOURCE = ('(SELECT r.*, s.saved_at AS accepted_at FROM ' + contracts.REQUESTS + ' r JOIN '
    + contracts.STAGES + " s ON s.operation_id=r.operation_id AND s.stage='link_intent')")


def _contract_sources(conn, *, selected, request_scope, supplier_safe):
    if supplier_safe or not request_scope or contracts.DOMAIN not in selected or not trade.exists(conn, contracts.REQUESTS):
        return []
    return [(CONTRACT_SOURCE, 'operation_id', '*',
        "request_scope=? AND action IN ('upload_link','link','unlink')", (request_scope,),
        lambda connection, row: contracts._public(connection, row)['acceptance'])]


def journal(db_path, *, page=1, limit=25, allowed_domains=None, allowed_sections=None, domain='all', search='', request_scope='', supplier_safe=False, runtime_dir=None, actor='', external_scope=None, feedback_scope=None, settings_scope=None, ai_settings_scope=None, analysis_settings_scope=None, complaint_schedules_scope=None, complaint_runs_scope=None, cleaner_scope=None, balance_scope=None, spp_scope=None):
    if type(page) is not int or type(limit) is not int or not 1<=page<=100000 or not 1<=limit<=100:
        raise ValueError('invalid_operation_journal_page')
    allowed = _allowed(allowed_domains, allowed_sections)
    if domain not in ('', 'all', *DOMAIN_LABELS):
        raise ValueError('invalid_operation_domain')
    search = str(search or '').strip()
    if len(search) > 200:
        raise ValueError('invalid_operation_search')
    selected = allowed if domain in ('', 'all') else allowed.intersection({domain})
    file_items = facilities.journal_entries(db_path, request_scope=request_scope) if request_scope and not supplier_safe and facilities.DOMAIN in selected else []
    with closing(overhead.readonly(db_path)) as conn:
        sources=supplier_journal.sources(conn, selected=selected, request_scope=request_scope, supplier_safe=supplier_safe, db_path=db_path, runtime_dir=runtime_dir)
        sources.extend(_trade_sources(conn, selected=selected, request_scope=request_scope, supplier_safe=supplier_safe))
        sources.extend(_contract_sources(conn, selected=selected, request_scope=request_scope, supplier_safe=supplier_safe))
        sources.extend(feedback.sources(conn,selected=selected,scope=feedback_scope))
        file_items.extend(feedback.complaint_items(selected=selected,scope=feedback_scope))
        file_items.extend(analysis_settings.items(selected=selected,scope=analysis_settings_scope))
        file_items.extend(complaint_schedules.items(selected=selected,scope=complaint_schedules_scope))
        file_items.extend(complaint_runs.items(selected=selected,scope=complaint_runs_scope))
        file_items.extend(spp_jobs.items(selected=selected,scope=spp_scope,db_path=db_path))
        external_source=external.source(conn,selected=selected,scope=external_scope,db_path=db_path)
        if external_source:sources.append(external_source)
        settings_source=business_settings.source(conn,selected=selected,scope=settings_scope)
        if settings_source:sources.append(settings_source)
        ai_source=ai_settings.source(conn,selected=selected,scope=ai_settings_scope)
        if ai_source:sources.append(ai_source)
        cleaner_source=cleaner.source(conn,selected=selected,scope=cleaner_scope,db_path=db_path)
        if cleaner_source:sources.append(cleaner_source)
        balance_source=balance_jobs.source(conn,selected=selected,scope=balance_scope,db_path=db_path)
        if balance_source:sources.append(balance_source)
        if actor and nomenclature.DOMAIN in selected and nomenclature.exists(conn):
            sources.append((nomenclature.TABLE, 'operation_id', '*', 'actor=?', (actor,), nomenclature.public))
        if 'ff_pool_document' in selected:
            if overhead._exists(conn):
                sources.append((overhead.TABLE, 'request_id', '*', '1', (), _overhead_public))
            if warehouse.exists(conn):
                sources.append((warehouse.TABLE, 'request_id', '*', '1', (), warehouse._public))
        if fulfillment.DOMAIN in selected and fulfillment._exists(conn, fulfillment.TABLE):
            sources.append((fulfillment.TABLE, 'operation_id', '*', '1', (), fulfillment._public))
        compatibility = tuple(sorted(selected.intersection(compat_uploads.DOMAINS)))
        if compatibility and compat_uploads.exists(conn):
            sources.append((compat_uploads.TABLE, 'operation_id', '*',
                'domain IN (' + ','.join('?' for _ in compatibility) + ')', compatibility, compat_uploads.public))
        reports = tuple(sorted(selected.intersection(report_sources.DOMAINS)))
        if partner_report.DOMAIN in selected and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (partner_report.TABLE,)).fetchone():
            sources.append((partner_report.TABLE, 'operation_id', '*', '1', (), partner_report.public))
        if reports and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (report_sources.TABLE,)).fetchone():
            sources.append((report_sources.TABLE, 'operation_id', report_sources.PUBLIC_COLUMNS,
                'domain IN (' + ','.join('?' for _ in reports) + ')', reports,
                lambda connection, row: report_sources.public(dict(row))))
        if search:
            file_items=[item for item in file_items if search.casefold() in json_search(item).casefold()]
            value = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            search_columns = {report_sources.TABLE: 'after_json', partner_report.TABLE: "product_name || ' ' || nm_id", external.TABLE: external.SEARCH_COLUMNS}
            search_columns.update({feedback.AUTO_TABLE: "processing_key || ' ' || feedback_id", feedback.BUYER_TABLE: "id || ' ' || kind || ' ' || item_id"})
            search_columns[ai_settings.TABLE] = "event_id || ' ' || json_extract(details_json,'$.saved_settings.mode')"
            search_columns[trade.TABLE] = "coalesce(json_extract(source_json,'$.document.number'),'') || ' ' || coalesce(json_extract(source_json,'$.document.file_original_name'),'') || ' ' || action"
            search_columns[business_settings.TABLE] = "operation_id || ' ' || config_key"
            search_columns[cleaner.TABLE] = "request_id || ' ' || route"
            search_columns[CONTRACT_SOURCE] = "shipment_id || ' ' || action || ' ' || coalesce(json_extract(order_json,'$.header.invoice_no'),'')"
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
            search_columns[balance_jobs.TABLE] = "client_request_id || ' ' || calculation_id || ' ' || job_id"
            sources = [(table, key, columns, where + " AND lower(" + search_columns.get(table, 'source_json') + ") LIKE lower(?) ESCAPE '\\'",
                        (*params, value), reader) for table, key, columns, where, params, reader in sources]
        total = sum(conn.execute(f'SELECT count(*) FROM {table} WHERE {where}', params).fetchone()[0]
                    for table, _, _, where, params, _ in sources)+len(file_items)
        # Distinct action families may share one native table. Keep the
        # exact scoped reader paired with its own UNION arm.
        sql = ' UNION ALL '.join(f"SELECT {identity} AS operation_id,{'created_at' if table in (external.TABLE,balance_jobs.TABLE) else 'accepted_at'} AS accepted_at,'{index}' source_table FROM {table} WHERE {where}"
            for index, (table, identity, _, where, _, _) in enumerate(sources))
        params = tuple(value for _, _, _, _, values, _ in sources for value in values)
        offset=(page-1)*limit
        db_offset=max(0,offset-len(file_items))
        rows = conn.execute('SELECT * FROM (' + sql + ') ORDER BY accepted_at DESC,operation_id DESC LIMIT ? OFFSET ?',
            (*params, limit+len(file_items), db_offset)).fetchall() if sources else []
        readers = {str(index): item for index, item in enumerate(sources)}
        items = []
        for row in rows:
            table, identity, columns, where, values, reader = readers[row['source_table']]
            native = conn.execute(f'SELECT {columns} FROM {table} WHERE {identity}=? AND ({where})',
                                  (row['operation_id'], *values)).fetchone()
            items.append(_common(reader(conn, native)))
        items.extend(_common(item) for item in file_items)
        items.sort(key=lambda item:(item['accepted_at'],item['operation_id']),reverse=True)
        items=items[offset-db_offset:offset-db_offset+limit]
    return {'contract_name':'operator_operations_v1','status':'ready','items':items,'page':page,'limit':limit,
        'total':total,'has_more':page*limit<total,
        'available_domains':[{'domain':key,'label_ru':DOMAIN_LABELS[key]} for key in sorted(allowed)]}


def json_search(item):
    """Only public identifiers/fields participate in file-source search."""
    values = (item.get('operation_id'), item.get('actor'), item.get('native_state'),
              item.get('source_ref', {}).get('entity_id'), item.get('title_ru'))
    return ' '.join(str(value or '') for value in values)


def read_acceptance(db_path, identity, *, allowed_domains=None, allowed_sections=None, domain='', request_scope='', supplier_safe=False, runtime_dir=None, actor='', external_scope=None, feedback_scope=None, settings_scope=None, ai_settings_scope=None, analysis_settings_scope=None, complaint_schedules_scope=None, complaint_runs_scope=None, cleaner_scope=None, balance_scope=None, spp_scope=None):
    allowed = _allowed(allowed_domains, allowed_sections)
    if domain:
        if domain not in DOMAIN_LABELS:
            raise ValueError('invalid_operation_domain')
        allowed.intersection_update({domain})
    spp_receipt=spp_jobs.read(identity=identity,selected=allowed,scope=spp_scope,db_path=db_path)
    if spp_receipt:return _common(spp_receipt)
    with closing(overhead.readonly(db_path)) as conn:
        for item in analysis_settings.items(selected=allowed,scope=analysis_settings_scope):
            if item['operation_id']==identity:return _common(item)
        if request_scope and not supplier_safe and facilities.DOMAIN in allowed:
            result = facilities.read(db_path, request_scope=request_scope, operation_id=identity)
            if result.get('status') == 'accepted' and result.get('acceptance'):
                return _common(result['acceptance'])
        spec=balance_jobs.source(conn,selected=allowed,scope=balance_scope,db_path=db_path)
        if spec:
            table,key,columns,where,values,reader=spec
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})',(identity,*values)).fetchone()
            if row:return _common(reader(conn,row))
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
        spec=cleaner.source(conn,selected=allowed,scope=cleaner_scope,db_path=db_path)
        if spec:
            table,key,columns,where,values,reader=spec
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})',(identity,*values)).fetchone()
            if row:return _common(reader(conn,row))
        for table,key,columns,where,values,reader in feedback.sources(conn,selected=allowed,scope=feedback_scope):
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})',(identity,*values)).fetchone()
            if row:return _common(reader(conn,row))
        for item in complaint_runs.items(selected=allowed,scope=complaint_runs_scope):
            if item['operation_id']==identity:return _common(item)
        for item in complaint_schedules.items(selected=allowed,scope=complaint_schedules_scope):
            if item['operation_id']==identity:return _common(item)
        for item in feedback.complaint_items(selected=allowed,scope=feedback_scope):
            if item['operation_id']==identity:return _common(item)
        spec=ai_settings.source(conn,selected=allowed,scope=ai_settings_scope)
        if spec:
            table,key,columns,where,values,reader=spec
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})',(identity,*values)).fetchone()
            if row:return _common(reader(conn,row))
        spec=external.source(conn,selected=allowed,scope=external_scope,db_path=db_path)
        if spec:
            table,key,columns,where,values,reader=spec
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})',(identity,*values)).fetchone()
            if row:return _common(reader(conn,row))
        spec=business_settings.source(conn,selected=allowed,scope=settings_scope)
        if spec:
            table,key,columns,where,values,reader=spec
            row=conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})',(identity,*values)).fetchone()
            if row:return _common(reader(conn,row))
        if actor and nomenclature.DOMAIN in allowed and nomenclature.exists(conn):
            row = conn.execute(f'SELECT * FROM {nomenclature.TABLE} WHERE operation_id=? AND actor=?', (identity, actor)).fetchone()
            if row is not None:
                return _common(nomenclature.public(conn, row))
        if fulfillment.DOMAIN in allowed and fulfillment._exists(conn, fulfillment.TABLE):
            row = conn.execute(f'SELECT * FROM {fulfillment.TABLE} WHERE operation_id=?', (identity,)).fetchone()
            if row:
                return _common(fulfillment._public(conn, row))
        for table, key, columns, where, values, reader in (
                _trade_sources(conn, selected=allowed, request_scope=request_scope, supplier_safe=supplier_safe)
                + _contract_sources(conn, selected=allowed, request_scope=request_scope, supplier_safe=supplier_safe)):
            row = conn.execute(f'SELECT {columns} FROM {table} WHERE {key}=? AND ({where})', (identity, *values)).fetchone()
            if row is not None:
                return _common(reader(conn, row))
        if 'ff_pool_document' in allowed:
            canonical=overhead._resolve(conn,identity) if (overhead._exists(conn) or warehouse.exists(conn)) else identity
            for table,reader in ((overhead.TABLE,_overhead_public),(warehouse.TABLE,warehouse._public)):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():
                    row=conn.execute(f'SELECT * FROM {table} WHERE request_id=?',(canonical,)).fetchone()
                    if row:
                        return _common(reader(conn,row))
        compatibility = tuple(sorted(allowed.intersection(compat_uploads.DOMAINS)))
        if compatibility and compat_uploads.exists(conn):
            row = conn.execute(f"SELECT * FROM {compat_uploads.TABLE} WHERE operation_id=? AND domain IN ({','.join('?' for _ in compatibility)})", (identity, *compatibility)).fetchone()
            if row is not None:
                return _common(compat_uploads.public(conn, row))
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
