"""Scoped read-only supplier sources for the common operation journal."""
from packages.application import operator_supplier_shipments as shipments
from packages.application import operator_supplier_factual_dates as factual
from packages.application import operator_supplier_financial as financial

LABELS = {shipments.DOMAIN: 'Заказы поставщикам', factual.DOMAIN: 'Фактические даты поставок',
          financial.DOMAIN: 'Финансовые документы поставок'}
FINANCIAL_ACTIONS = tuple(sorted(financial.FINANCIAL_ACTIONS))


def financial_acceptance(conn, runtime_dir, db_path, identity, *, request_scope):
    if not shipments._exists(conn, financial.REQUESTS) or not shipments._exists(conn, financial.CHILDREN):
        return None
    row = conn.execute(f'SELECT action FROM {financial.REQUESTS} WHERE request_scope=? AND '
        f'(operation_id=? OR request_id=(SELECT request_id FROM {financial.CHILDREN} WHERE operation_id=? AND request_scope=?))',
        (request_scope, identity, identity, request_scope)).fetchone()
    if row is None or row['action'] not in FINANCIAL_ACTIONS:
        return None
    value = financial.read_operation(runtime_dir, db_path, identity, request_scope=request_scope).get('acceptance')
    return value if value and value['domain'] == financial.DOMAIN and value['operation_id'] == identity else None


def sources(conn, *, selected, request_scope, supplier_safe, db_path, runtime_dir=None):
    if not request_scope:
        return []
    result = []
    if shipments.DOMAIN in selected and shipments._exists(conn, shipments.TABLE):
        result.append((shipments.TABLE, 'operation_id', '*', 'request_scope=?', (request_scope,),
            lambda connection, row: shipments.public(connection, row, supplier_safe=supplier_safe)))
    if not supplier_safe and factual.DOMAIN in selected and shipments._exists(conn, factual.TABLE):
        # Native deduplication may bind several request aliases to one correction.
        # The common journal presents that exact operation once within its scope.
        where = ("request_scope=? AND status='accepted' AND request_id=(SELECT MIN(alias.request_id) "
                 f"FROM {factual.TABLE} alias WHERE alias.correction_id={factual.TABLE}.correction_id "
                 f"AND alias.request_scope={factual.TABLE}.request_scope AND alias.status='accepted')")
        result.append((factual.TABLE, 'correction_id', '*', where, (request_scope,),
            lambda connection, row: factual.read_operation(db_path, row['correction_id'],
                request_scope=request_scope)['acceptance']))
    if (not supplier_safe and runtime_dir is not None and financial.DOMAIN in selected
            and shipments._exists(conn, financial.REQUESTS) and shipments._exists(conn, financial.CHILDREN)):
        # An admitted batch or parse preview is not a saved business document.
        # List one parent only; accepted children keep their native exact IDs.
        where = ('request_scope=? AND action IN (' + ','.join('?' for _ in FINANCIAL_ACTIONS) + ') AND EXISTS (SELECT 1 FROM ' + financial.CHILDREN + ' child '
            'WHERE child.request_scope=' + financial.REQUESTS + '.request_scope '
            'AND child.request_id=' + financial.REQUESTS + '.request_id)')
        result.append((financial.REQUESTS, 'operation_id', '*', where, (request_scope, *FINANCIAL_ACTIONS),
            lambda connection, row: financial_acceptance(connection, runtime_dir, db_path, row['operation_id'],
                request_scope=request_scope)))
    return result
