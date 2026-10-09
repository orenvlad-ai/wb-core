"""Immutable operator intent; native FF documents and jobs own every effect.

Acceptance does not certify physical application. Only a native transaction
applies stock, and only exact published operands certify derived completion.
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import sqlite3

from packages.application.operator_ff_overhead import readonly, _resolve

TABLE = "sheet_vitrina_v1_ff_pool_operator_confirmations"
KINDS = ("china_acceptance", "transfer_root", "transfer_shipment", "transfer_receipt",
         "transfer_loss", "transfer_discrepancy", "transfer_cancellation", "pool_reallocation",
         "pool_inventory", "correction", "storno", "late_expense")
SOURCE_KEYS = ("request_identity", "source_revision", "source_sha256", "business_date", "document_kind", "idempotency_epoch")


def ensure_schema(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TABLE}(
            request_id TEXT PRIMARY KEY REFERENCES sheet_vitrina_v1_ff_pool_document_requests(request_id),
            source_json TEXT NOT NULL CHECK(json_valid(source_json)),source_digest TEXT NOT NULL,
            accepted_at TEXT NOT NULL,actor TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('accepted','processing','completed','delayed','needs_attention')),
            updated_at TEXT NOT NULL,reason_code TEXT NOT NULL DEFAULT '',
            receipt_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(receipt_json)));
        CREATE INDEX IF NOT EXISTS ff_operator_confirmations_time ON {TABLE}(accepted_at,request_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ff_operator_guided_single_source ON {TABLE}(json_extract(source_json,'$.source_id'))
            WHERE json_extract(source_json,'$.document_kind')='china_acceptance';
        CREATE UNIQUE INDEX IF NOT EXISTS ff_operator_single_storno_target ON {TABLE}(json_extract(source_json,'$.manifest.target_document_id'))
            WHERE json_extract(source_json,'$.document_kind')='storno';
        CREATE TRIGGER IF NOT EXISTS ff_operator_confirmation_immutable BEFORE UPDATE ON {TABLE}
        WHEN NEW.request_id IS NOT OLD.request_id OR NEW.source_json IS NOT OLD.source_json
          OR NEW.source_digest IS NOT OLD.source_digest OR NEW.accepted_at IS NOT OLD.accepted_at OR NEW.actor IS NOT OLD.actor
        BEGIN SELECT RAISE(ABORT,'confirmed warehouse source is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_operator_confirmation_no_delete BEFORE DELETE ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'confirmed warehouse source is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_operator_confirmed_request_immutable
        BEFORE UPDATE ON sheet_vitrina_v1_ff_pool_document_requests
        WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE request_id=OLD.request_id)
          AND (NEW.request_id IS NOT OLD.request_id OR NEW.request_payload_json IS NOT OLD.request_payload_json OR NEW.request_identity IS NOT OLD.request_identity
            OR NEW.business_date IS NOT OLD.business_date OR NEW.source_revision IS NOT OLD.source_revision
            OR NEW.source_sha256 IS NOT OLD.source_sha256 OR NEW.source_file_blob IS NOT OLD.source_file_blob
            OR NEW.source_id IS NOT OLD.source_id OR NEW.source_type IS NOT OLD.source_type
            OR NEW.source_system IS NOT OLD.source_system OR NEW.idempotency_epoch IS NOT OLD.idempotency_epoch
            OR NEW.actor IS NOT OLD.actor OR NEW.document_kind IS NOT OLD.document_kind
            OR NEW.source_filename IS NOT OLD.source_filename OR NEW.source_content_type IS NOT OLD.source_content_type)
        BEGIN SELECT RAISE(ABORT,'confirmed warehouse request source is immutable'); END;
    """)


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (TABLE,)).fetchone() is not None


def effect(plan):
    """Freeze money, quantities, locations and parents; exclude volatile before-images."""
    return {"primary_document_id": plan["primary_document_id"], "root_document_id": plan["root_document_id"],
            "documents": plan["documents"]}


def assert_source(conn, request):
    from packages.application.ff_pool_documents import _fingerprint, FfPoolDocumentError
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_id=?", (request["request_id"],)).fetchone()
    source = json.loads(row["source_json"]) if row else {}
    if (not row or _fingerprint(source) != row["source_digest"]
            or any(request[k] != source[k] for k in SOURCE_KEYS)
            or json.loads(request["request_payload_json"]) != source["manifest"]):
        raise FfPoolDocumentError("operator_source_changed", "Подтверждённый источник изменился")
    return source


def assert_accepted_epoch(conn, request):
    """A confirmed source never migrates to another writer epoch."""
    if exists(conn) and conn.execute(f"SELECT 1 FROM {TABLE} WHERE request_id=?", (request['request_id'],)).fetchone():
        from packages.application.ff_pool_documents import _writer_epoch, FfPoolDocumentError
        source=assert_source(conn,request)
        if _writer_epoch(conn) != source['idempotency_epoch']:
            raise FfPoolDocumentError('feature_epoch_changed','Складской режим изменился после подтверждения')


def inventory_snapshot(conn, manifest, epoch):
    """Full active identity roster and both pools; no derived publication dependency."""
    from packages.application.ff_pool_documents import _fingerprint, _validate_manifest, _scope, _positive_int, _nonnegative_int, FfPoolDocumentError
    _validate_manifest('pool_inventory',manifest)
    roster = [dict(r) for r in conn.execute("SELECT item_id,nm_id,barcode,barcodes_json FROM sheet_vitrina_v1_nomenclature_items WHERE is_active=1 AND is_hidden=0 AND nm_id IS NOT NULL ORDER BY nm_id,item_id")]
    nm_ids = [int(r['nm_id']) for r in roster]
    if not nm_ids or len(nm_ids) != len(set(nm_ids)):
        raise FfPoolDocumentError('inventory_roster_ambiguous','Активный состав номенклатуры отсутствует или неоднозначен')
    targets = manifest.get('targets', [])
    if not isinstance(targets,list) or any(not isinstance(t,dict) for t in targets):
        raise FfPoolDocumentError('inventory_targets_required','Нужна таблица целевых остатков')
    if sorted(_positive_int(t.get('nm_id'),field='inventory nm_id') for t in targets) != nm_ids:
        raise FfPoolDocumentError('inventory_incomplete_active_roster','Инвентаризация должна содержать весь активный состав, включая явные нули')
    scope=_scope(str(manifest['scope']))
    pools = ('FBS', 'FBO') if scope == 'both' else (scope,)
    for target in targets:
        for pool in pools:
            value = target.get('target_' + pool.lower())
            try:
                _nonnegative_int(value,field='inventory target '+pool)
            except FfPoolDocumentError as exc:
                raise FfPoolDocumentError('inventory_explicit_target_required','Для каждого товара нужен явный целый остаток, включая ноль') from exc
    rows = []
    for nm in nm_ids:
        for pool in ('FBS', 'FBO'):
            row = conn.execute("SELECT quantity,capital_rub,wac_rub,source_watermark FROM sheet_vitrina_v1_ff_pool_balances WHERE facility_id=? AND pool=? AND nm_id=? AND projection_epoch=?", (manifest['facility_id'],pool,nm,epoch)).fetchone()
            rows.append({'nm_id':nm,'pool':pool,'balance':dict(row) if row else None})
    return {'roster_digest':_fingerprint(roster),'facility_id':manifest['facility_id'],'epoch':epoch,'rows':rows}


def pin_inventory_manifest(conn, manifest, epoch):
    """Pin documentary surplus prices before the operator reviews the preview."""
    from packages.application.ff_pool_documents import _fingerprint, _scope, canonical_decimal_text
    manifest['scope']=_scope(str(manifest.get('scope') or ''))
    manifest['operator_inventory_snapshot']=inventory_snapshot(conn,manifest,epoch)
    bases={str(k):v for k,v in (manifest.get('cost_basis_by_nm') or {}).items()}
    from packages.application.ff_pool_documents import _inventory_cost_basis
    for target in manifest['targets']:
        nm=int(target['nm_id'])
        if isinstance(bases.get(str(nm)),dict):
            continue  # explicitly authorized existing native basis contract
        pools=('FBS','FBO') if manifest['scope']=='both' else (manifest['scope'],)
        before={r['pool']:r['balance'] for r in manifest['operator_inventory_snapshot']['rows'] if r['nm_id']==nm}
        if not any(int(target['target_'+pool.lower()])>int((before[pool] or {}).get('quantity',0)) for pool in pools):
            continue
        rows=[dict(r) for r in conn.execute("SELECT pool,quantity,capital_rub,source_watermark FROM sheet_vitrina_v1_ff_pool_balances WHERE projection_epoch=? AND facility_id=? AND nm_id=? AND quantity>0 ORDER BY pool",(epoch,manifest['facility_id'],nm))]
        unit=_inventory_cost_basis(conn,facility_id=manifest['facility_id'],nm_id=nm,epoch=epoch,explicit=bases.get(str(nm)))
        bases[str(nm)]={'unit_cost_rub':canonical_decimal_text(unit),
                        'source_digest':_fingerprint({'epoch':epoch,'facility_id':manifest['facility_id'],'nm_id':nm,'rows':rows})}
    manifest['cost_basis_by_nm']=bases
    return manifest


def assert_inventory_inputs(conn, request):
    if request['document_kind'] != 'pool_inventory':
        return
    from packages.application.ff_pool_documents import FfPoolDocumentError
    manifest=json.loads(request['request_payload_json'])
    pinned=manifest.get('operator_inventory_snapshot')
    if not pinned and exists(conn):
        row=conn.execute(f'SELECT source_json FROM {TABLE} WHERE request_id=?',(request['request_id'],)).fetchone()
        pinned=json.loads(row['source_json']).get('inventory_input_snapshot') if row else None
    if pinned and inventory_snapshot(conn,manifest,request['idempotency_epoch']) != pinned:
        raise FfPoolDocumentError('inventory_source_prestate_changed','Состав или исходные остатки изменились после проверки инвентаризации')


def affected_nm_ids(plan):
    # Root inventories, open-transit late expenses and evidence-only reversals
    # still own documentary cost operands even when they have no movement.
    movements={int(m['nm_id']) for d in plan['documents'] for m in d.get('movements',[])}
    if any(d['document_kind'] in {'pool_inventory','correction','storno','late_expense'} for d in plan['documents']):
        movements |= {int(l['nm_id']) for d in plan['documents'] for l in d.get('lines',[]) if l.get('nm_id')}
    return sorted(movements)


def assert_effect(conn, request, plan, *, before_physical=True):
    if exists(conn) and conn.execute(f"SELECT 1 FROM {TABLE} WHERE request_id=?", (request["request_id"],)).fetchone():
        from packages.application.ff_pool_documents import _fingerprint, FfPoolDocumentError
        source = assert_source(conn, request)
        if before_physical:
            assert_inventory_inputs(conn,request)
        if source.get('native_recovery') and source['native_recovery'] != plan.get('domain_manifest',{}).get('guided_acceptance_recovery'):
            raise FfPoolDocumentError('operator_authorized_effect_changed','Основание восстановления приёмки изменилось')
        if _fingerprint(effect(plan)) != source["effect_digest"]:
            raise FfPoolDocumentError("operator_authorized_effect_changed", "Количество, стоимость или основание подтверждённого движения изменились")


def assert_supplier_inputs(conn, request):
    if not exists(conn) or not conn.execute(f'SELECT 1 FROM {TABLE} WHERE request_id=?',(request['request_id'],)).fetchone():
        return
    source=assert_source(conn,request)
    if source.get('supplier_input_snapshot') and _supplier_input_snapshot(conn,request['source_id']) != source['supplier_input_snapshot']:
        from packages.application.ff_pool_documents import FfPoolDocumentError
        raise FfPoolDocumentError('supplier_source_revision_changed','Поставка или её стоимость изменились после подтверждения')


def assert_reservations(conn, *, plan, epoch):
    """Recheck exact authoritative reservations in the physical writer transaction."""
    from packages.application.ff_pool_documents import FfPoolDocumentError
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    debits = {}
    for doc in plan["documents"]:
        for movement in doc.get("movements", []):
            if movement["quantity_delta"] < 0:
                key = (movement["facility_id"], movement["pool"], movement["nm_id"])
                debits[key] = debits.get(key, 0) - movement["quantity_delta"]
    for (facility, pool, nm), debit in debits.items():
        balance = conn.execute("SELECT quantity FROM sheet_vitrina_v1_ff_pool_balances WHERE facility_id=? AND pool=? AND nm_id=? AND projection_epoch=?",
                               (facility, pool, nm, epoch)).fetchone()
        physical = int(balance[0]) if balance else 0
        reserved = Decimal(0)
        if pool == 'FBS':
            from packages.application.wb_fbs_orders import OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE, WAREHOUSE_MAPPINGS_TABLE
            from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE, RECONCILIATION_TABLE, EVENTS_TABLE
            if {OBSERVATIONS_TABLE,STATUS_CURRENT_TABLE,WAREHOUSE_MAPPINGS_TABLE,CURRENT_TABLE} <= tables:
                outstanding=conn.execute(f"""SELECT observation.order_id,COALESCE(lifecycle.state,'') state
                    FROM {OBSERVATIONS_TABLE} observation
                    JOIN {WAREHOUSE_MAPPINGS_TABLE} mapping ON mapping.seller_warehouse_id=observation.warehouse_id AND mapping.active=1
                    LEFT JOIN {STATUS_CURRENT_TABLE} status USING(order_id)
                    LEFT JOIN {CURRENT_TABLE} lifecycle ON lifecycle.order_id=observation.order_id AND lifecycle.facility_id=mapping.facility_id AND lifecycle.pool='FBS'
                    WHERE mapping.facility_id=? AND observation.nm_id=?
                      AND observation.observation_sequence=(SELECT MAX(newer.observation_sequence) FROM {OBSERVATIONS_TABLE} newer WHERE newer.order_id=observation.order_id)
                      AND COALESCE(status.supplier_status,'')<>'cancel'
                      AND COALESCE(status.wb_status,'') NOT IN ('sold','accepted_by_client','canceled','canceled_by_client','declined_by_client','defect')
                      AND COALESCE(lifecycle.state,'') NOT IN ('reserved','released','fulfilled','fulfilled_reconciliation','cancelled_noop','late_pre_t_isolated') LIMIT 1""",(facility,nm)).fetchone()
                if outstanding:
                    raise FfPoolDocumentError('fbs_order_dependency_unresolved','Действующий заказ ещё не связан с точным резервом')
            if {RECONCILIATION_TABLE,EVENTS_TABLE} <= tables and conn.execute(f"SELECT 1 FROM {RECONCILIATION_TABLE} lane JOIN {EVENTS_TABLE} event USING(event_id) WHERE lane.state='open' AND event.facility_id=? AND event.nm_id=? LIMIT 1",(facility,nm)).fetchone():
                raise FfPoolDocumentError('fbs_reconciliation_unresolved','Для товара открыто расхождение заказа')
        if pool == "FBS" and "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current" in tables:
            manifest_table="sheet_vitrina_v1_ff_pool_cutover_manifests"
            if manifest_table not in tables:
                raise FfPoolDocumentError('fbs_order_dependency_unresolved','Эпоха действующего резерва ещё не подтверждена')
            if conn.execute("""SELECT 1 FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_current current
                LEFT JOIN sheet_vitrina_v1_ff_pool_cutover_manifests manifest USING(cutover_id)
                WHERE current.facility_id=? AND current.nm_id=? AND current.pool='FBS' AND current.state='reserved'
                  AND manifest.cutover_id IS NULL LIMIT 1""",(facility,nm)).fetchone():
                raise FfPoolDocumentError('fbs_order_dependency_unresolved','Эпоха действующего резерва ещё не подтверждена')
            reserved = sum((Decimal(str(r[0])) for r in conn.execute("""SELECT current.quantity
                FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_current current
                JOIN sheet_vitrina_v1_ff_pool_cutover_manifests manifest USING(cutover_id)
                WHERE manifest.feature_epoch=? AND current.facility_id=? AND current.nm_id=? AND current.pool='FBS' AND current.state='reserved'""", (epoch, facility, nm))), Decimal(0))
        if pool == "FBO" and {"sheet_vitrina_v1_ff_stock_reservation_operations", "sheet_vitrina_v1_ff_stock_reservation_lines"} <= tables:
            reservations = conn.execute("""SELECT operation.supply_id,SUM(line.quantity_delta) quantity
                FROM sheet_vitrina_v1_ff_stock_reservation_operations operation
                JOIN sheet_vitrina_v1_ff_stock_reservation_lines line USING(operation_id)
                WHERE line.nm_id=? GROUP BY operation.supply_id HAVING SUM(line.quantity_delta)>0""", (nm,)).fetchall()
            for reservation in reservations:
                origins = conn.execute("""SELECT origin.facility_id,origin.pool FROM sheet_vitrina_v1_wb_supply_ff_origin_assignments origin
                    WHERE origin.feature_epoch=? AND (origin.wb_supply_id=? OR origin.wb_supply_cache_key=?)
                      AND NOT EXISTS(SELECT 1 FROM sheet_vitrina_v1_wb_supply_ff_origin_assignments successor
                        WHERE successor.supersedes_assignment_id=origin.assignment_id)""", (epoch, reservation[0], reservation[0])).fetchall() if "sheet_vitrina_v1_wb_supply_ff_origin_assignments" in tables else []
                if len(origins) != 1:
                    raise FfPoolDocumentError("reservation_origin_unresolved", "Склад действующего резерва не подтверждён")
                if tuple(origins[0]) == (facility, pool):
                    reserved += Decimal(str(reservation[1]))
        if Decimal(physical - debit) < reserved:
            raise FfPoolDocumentError("reserved_stock_unavailable", "Движение затрагивает зарезервированный товар",
                                      details={"facility_id": facility, "pool": pool, "nm_id": nm,
                                               "physical": physical, "reserved": str(reserved), "requested": debit})


def _supplier_input_snapshot(conn, shipment_id):
    """Exact native cost inputs and canonical identity/overlapping barcode guards."""
    from packages.application.ff_pool_documents import _fingerprint
    from packages.application.our_wb_costs import (_selected_supplier_header_inputs, _selected_supplier_line_inputs,
        _selected_financial_document_inputs, _selected_expense_line_inputs)
    from packages.application.registry_upload_db_backed_runtime import (_supplier_shipment_header_to_dict,
        _supplier_shipment_line_to_dict, _supplier_financial_document_to_dict, _supplier_financial_expense_line_to_dict)
    from packages.application.ff_pool_surfaces import _canonical_nomenclature_identities
    header=conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?',(shipment_id,)).fetchone()
    lines=conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines WHERE shipment_id=? ORDER BY sort_order,line_id',(shipment_id,)).fetchall()
    documents=[_supplier_financial_document_to_dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_financial_documents WHERE supplier_order_id=? ORDER BY document_date DESC,uploaded_at DESC,document_id',(shipment_id,))]
    documents=[d for d in documents if d.get('parse_status')!='excluded']
    document_ids={d['document_id'] for d in documents}
    expenses=[_supplier_financial_expense_line_to_dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines WHERE supplier_order_id=? ORDER BY financial_document_id,sort_order,line_id',(shipment_id,)) if r['financial_document_id'] in document_ids]
    target_nm_ids={int(r['internal_nm_id']) for r in lines if r['line_type']=='product'}
    catalog=conn.execute('SELECT item_id,nm_id,barcode,barcodes_json FROM sheet_vitrina_v1_nomenclature_items WHERE is_active=1 AND is_hidden=0 ORDER BY nm_id,item_id').fetchall()
    # Canonical resolver checks only used identities and their barcode owners.
    # An unrelated SKU/name/time change does not become a source revision.
    identities=_canonical_nomenclature_identities(catalog,target_nm_ids=target_nm_ids)
    return _fingerprint({'header':_selected_supplier_header_inputs(_supplier_shipment_header_to_dict(header)),
        'state':{k:header[k] for k in ('shipment_id','updated_at','archived_at','order_status','actual_ff_acceptance_date')},
        'lines':[_selected_supplier_line_inputs(_supplier_shipment_line_to_dict(r)) for r in lines],
        'documents':[_selected_financial_document_inputs(d) for d in documents],
        'expenses':[_selected_expense_line_inputs(e) for e in expenses],'identities':identities})


def confirm_source(surface, identity, *, actor=None):
    from packages.application.ff_pool_documents import (_connect, REQUESTS_TABLE, _fingerprint, _build_posting_plan,
        _validate_manifest, _validate_physical_plan, _writer_epoch, _is_guided_china_request, _guided_request_source_revision, _require_guided_acceptance_activation, FfPoolDocumentError)
    from packages.application.warehouse_domain_write_guard import assert_warehouse_domain_write_allowed
    from packages.application.ff_pool_surfaces import _actor
    # Readback first: retries after factual posting must not revalidate an already received shipment.
    if read_acceptance(surface.db_path, identity):
        return
    with closing(readonly(surface.db_path)) as conn:
        request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (_resolve(conn, identity),)).fetchone()
        if request is None or request["document_kind"] not in KINDS:
            raise FfPoolDocumentError("operator_request_not_found", "Сохранённый запрос не найден")
    verified_shipment = None
    supplier_snapshot = None
    if _is_guided_china_request(request):
        with closing(readonly(surface.db_path)) as conn:
            supplier_snapshot = _supplier_input_snapshot(conn,request['source_id'])
        shipment, _lines, revision = surface.supplier_shipment_source(request["source_id"])
        if request["source_revision"] != _guided_request_source_revision(supplier_source_revision=revision, source_sha256=request["source_sha256"]):
            raise FfPoolDocumentError("supplier_source_revision_changed", "Поставка или её стоимость изменились после проверки")
        verified_shipment = shipment
    with closing(_connect(surface.db_path)) as conn:
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("BEGIN IMMEDIATE")
        assert_warehouse_domain_write_allowed(conn, writer="ff_operator_confirmation")
        canonical = _resolve(conn, identity)
        if conn.execute(f"SELECT 1 FROM {TABLE} WHERE request_id=?", (canonical,)).fetchone():
            conn.rollback()
            return
        request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (canonical,)).fetchone()
        epoch = _writer_epoch(conn)
        if epoch != request["idempotency_epoch"]:
            raise FfPoolDocumentError("feature_epoch_changed", "Складской режим изменился после проверки")
        manifest = json.loads(request["request_payload_json"])
        _validate_manifest(request["document_kind"], manifest)
        now = surface._now()
        from packages.business_time import business_date_from_timestamp
        if request["business_date"] > business_date_from_timestamp(now):
            raise FfPoolDocumentError("acceptance_date_future", "Дата документа не может быть в будущем")
        if verified_shipment:
            _require_guided_acceptance_activation(conn)
            current = conn.execute("SELECT shipment_id,updated_at,archived_at,order_status,actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?", (request["source_id"],)).fetchone()
            if dict(current or {}) != verified_shipment or _supplier_input_snapshot(conn,request['source_id']) != supplier_snapshot:
                raise FfPoolDocumentError("supplier_source_revision_changed", "Поставка изменилась после проверки")
            prior = conn.execute(f"SELECT request_id FROM {TABLE} WHERE json_extract(source_json,'$.document_kind')='china_acceptance' AND json_extract(source_json,'$.source_id')=?",(request['source_id'],)).fetchone()
            if prior:
                raise FfPoolDocumentError('supplier_confirmation_exists','Приёмка этой поставки уже подтверждена; откройте сохранённую операцию',details={'request_id':prior[0]})
        assert_inventory_inputs(conn,request)
        if request['document_kind']=='pool_inventory' and not manifest.get('operator_inventory_snapshot'):
            inventory_snapshot(conn,manifest,epoch)  # old unconfirmed previews still require the full roster
        if request['document_kind']=='storno':
            prior=conn.execute(f"SELECT request_id FROM {TABLE} WHERE json_extract(source_json,'$.document_kind')='storno' AND json_extract(source_json,'$.manifest.target_document_id')=?",(manifest['target_document_id'],)).fetchone()
            if prior:
                raise FfPoolDocumentError('storno_confirmation_exists','Сторно этого документа уже подтверждено',details={'request_id':prior[0]})
        plan = _build_posting_plan(conn, request=request, manifest=manifest, epoch=epoch, intrinsic_only=True)
        _validate_physical_plan(conn,plan,epoch)
        try:
            assert_reservations(conn, plan=plan, epoch=epoch)
        except FfPoolDocumentError as exc:
            # Missing dependency evidence can be completed by the next collector.
            # It cannot authorize a debit: the actual native transaction repeats
            # this guard without an exception. Known reserve overspend rejects.
            if exc.code not in {'reservation_origin_unresolved','fbs_order_dependency_unresolved','fbs_reconciliation_unresolved'}:
                raise
        source = {k: request[k] for k in SOURCE_KEYS}
        source.update(manifest=manifest, effect_digest=_fingerprint(effect(plan)), effect=effect(plan),
                      filename=request["source_filename"], source_id=request["source_id"], source_type=request["source_type"], supplier_input_snapshot=supplier_snapshot, preview_actor=request['actor'],
                      native_recovery=plan.get('domain_manifest',{}).get('guided_acceptance_recovery') or {},
                      inventory_input_snapshot=inventory_snapshot(conn,manifest,epoch) if request['document_kind']=='pool_inventory' else None)
        conn.execute(f"INSERT INTO {TABLE}(request_id,source_json,source_digest,accepted_at,actor,state,updated_at) VALUES(?,?,?,?,?,'accepted',?)",
                     (canonical, json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':')), _fingerprint(source), now, _actor(actor if actor is not None else request['actor']), now))
        conn.commit()


def enqueue_posted(conn, *, request, plan, posted_at):
    if not exists(conn) or not conn.execute(f"SELECT 1 FROM {TABLE} WHERE request_id=?", (request["request_id"],)).fetchone():
        return
    from packages.application.ff_pool_documents import _fingerprint, TARGETED_RECALC_QUEUE_TABLE
    assert_effect(conn, request, plan, before_physical=False)
    nm_ids = affected_nm_ids(plan)
    if not nm_ids:
        return
    stable = "ff_pool_document:" + plan["primary_document_id"]
    revision = _fingerprint({"request_identity":request["request_identity"], "effect":effect(plan), "business_date":request["business_date"]})
    from packages.application.warehouse_functional import enqueue_source_replay_in_connection
    queued = enqueue_source_replay_in_connection(conn,stable_source_id=stable,source_revision=revision,
        effective_date=request['business_date'],affected_nm_ids_json=json.dumps(nm_ids),requested_at=posted_at)
    if queued['effective_date']!=request['business_date'] or json.loads(queued['affected_nm_ids_json'])!=nm_ids:
        raise ValueError('operator_targeted_identity_conflict')
    recovery=plan.get('domain_manifest',{}).get('guided_acceptance_recovery') or {}
    if recovery:
        extra=enqueue_source_replay_in_connection(conn,stable_source_id='supplier_shipment:'+recovery['shipment_id'],
            source_revision=request['request_identity'],effective_date=request['business_date'],
            affected_nm_ids_json=json.dumps(sorted(recovery['affected_nm_ids'])),requested_at=posted_at)
        if extra['effective_date']!=request['business_date'] or json.loads(extra['affected_nm_ids_json'])!=sorted(recovery['affected_nm_ids']):
            raise ValueError('operator_recovery_targeted_identity_conflict')



def _update(db_path, identity, state, reason, receipt=None):
    with sqlite3.connect(db_path, timeout=2) as conn:
        conn.execute(f"UPDATE {TABLE} SET state=?,reason_code=?,receipt_json=COALESCE(?,receipt_json),updated_at=? WHERE request_id=? AND state<>'completed'",
                     (state,reason,json.dumps(receipt,ensure_ascii=False) if receipt is not None else None,datetime.now(timezone.utc).isoformat(),identity))


def _public(conn, row):
    from packages.application.ff_pool_surfaces import DOCUMENT_LABELS_RU
    source = json.loads(row['source_json'])
    native = conn.execute("SELECT posted_document_id,state FROM sheet_vitrina_v1_ff_pool_document_requests WHERE request_id=?", (row['request_id'],)).fetchone()
    doc = conn.execute("SELECT document_id,root_document_id,posted_manifest_sha256 FROM sheet_vitrina_v1_ff_pool_documents WHERE document_id=?", (native['posted_document_id'],)).fetchone() if native and native['posted_document_id'] else None
    summary = {k:source['manifest'][k] for k in ('facility_id','source','destination','source_pool','destination_pool','root_document_id') if k in source['manifest']}
    summary['quantity'] = sum(l['quantity'] for d in source['effect']['documents'] if d['document_role']!='china_discrepancy' for l in d.get('lines', []))
    kind=source['document_kind']
    if kind in {'pool_inventory','correction','storno','late_expense'}:
        summary.pop('quantity',None)
        summary['child_document_ids']=[d['document_id'] for d in source['effect']['documents'] if d['document_id']!=source['effect']['primary_document_id']]
        if kind=='pool_inventory':
            summary.update(scope=source['manifest']['scope'],target_count=len(source['manifest']['targets']),
                surplus_quantity=sum(l['quantity'] for d in source['effect']['documents'] if d['document_kind']=='inventory_surplus' for l in d['lines']),
                shortage_quantity=sum(l['quantity'] for d in source['effect']['documents'] if d['document_kind']=='inventory_shortage' for l in d['lines']))
        elif kind=='correction':
            summary.update(target_document_id=source['manifest']['target_document_id'],
                quantity_delta=sum(m['quantity_delta'] for d in source['effect']['documents'] for m in d['movements']),
                capital_delta_rub=str(sum(Decimal(m['capital_delta_cents']) for d in source['effect']['documents'] for m in d['movements'])/100))
        elif kind=='storno':
            summary['target_document_id']=source['manifest']['target_document_id']
        else:
            summary['amount_rub']=str(sum(Decimal(e['amount_rub']) for e in source['manifest']['expenses']))
    fields=[{'label':'Дата документа','value':source['business_date']}]
    for label,key in [('Сегменты','scope'),('Товаров в инвентаризации','target_count'),('Излишек','surplus_quantity'),('Недостача','shortage_quantity'),('Изменение количества','quantity_delta'),('Изменение капитала, ₽','capital_delta_rub'),('Сумма, ₽','amount_rub'),('Исходный документ','target_document_id')]:
        if key in summary:
            fields.append({'label':label,'value':str(summary[key])})
    for label,key in [('Склад','facility_id'),('Исходный сегмент','source_pool'),('Целевой сегмент','destination_pool'),('Количество','quantity'),('Основание','root_document_id')]:
        if summary.get(key) is not None:
            value=summary[key]
            if key=='facility_id':
                facility=conn.execute('SELECT name FROM sheet_vitrina_v1_ff_facilities WHERE facility_id=?',(value,)).fetchone()
                value=facility[0] if facility else value
            fields.append({'label':label,'value':str(value)})
    for label,key in [('Откуда','source'),('Куда','destination')]:
        if summary.get(key):
            location=summary[key]
            facility=conn.execute('SELECT name FROM sheet_vitrina_v1_ff_facilities WHERE facility_id=?',(location['facility_id'],)).fetchone()
            fields.append({'label':label,'value':(facility[0] if facility else location['facility_id'])+' · '+location['pool']})
    labels = {'accepted':'Принято','processing':'Обрабатывается','completed':'Обработано','delayed':'Обработка отложена','needs_attention':'Требует внимания'}
    reasons = {'late_closed_period_document':'Документ принят прошлой датой. Закрытый день требует отдельной проверки учёта.',
               'operator_authorized_effect_changed':'Данные движения изменились после подтверждения. Документ сохранён и требует разбора.',
               'source_changed':'Подтверждённый источник изменился. Требуется разбор.',
               'publication_pending':'Документ применён. Ожидает подтверждения связанных расчётов.'}
    return {'durable_saved':True,'physical_applied':bool(doc),'operation_id':row['request_id'],'request_id':row['request_id'],
        'kind':source['document_kind'],'document_kind':source['document_kind'],'title':DOCUMENT_LABELS_RU[source['document_kind']], 'title_ru':DOCUMENT_LABELS_RU[source['document_kind']],
        'accepted_at':row['accepted_at'],'actor':row['actor'],'business_date':source['business_date'],'state':row['state'],'label_ru':labels[row['state']],
        'reason_code':row['reason_code'],'reason_ru':reasons.get(row['reason_code'],'Документ сохранён. Повторная отправка не требуется.'),
        'summary':summary,'fields':fields,'domain':'ff_pool_document','journal_path':'/sheet-vitrina-v1/operations?operation_id='+row['request_id'],
        'source_document':{'request_id':row['request_id'],'source_revision':source['source_revision'],'source_sha256':source['source_sha256'],'filename':source['filename']},
        'document':dict(doc) if doc else None,'processing_receipt':json.loads(row['receipt_json']),
        'detail_path':'/v1/sheet-vitrina-v1/operations/'+row['request_id'],
        'native_detail_path':'/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/requests/'+row['request_id']}


def read_acceptance(db_path, identity):
    with closing(readonly(db_path)) as conn:
        if not exists(conn):
            return None
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_id=?", (_resolve(conn,identity),)).fetchone()
        return _public(conn,row) if row else None


def _post(service, row):
    from packages.application.ff_pool_documents import _connect, REQUESTS_TABLE, _build_posting_plan, _writer_epoch, _posting_plan_preview, _is_guided_china_request
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
    with warehouse_functional_write_lock(service.runtime_dir, timeout_seconds=2):
        with closing(_connect(service.db_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (row['request_id'],)).fetchone()
            source = assert_source(conn, request)
            if request['posted_document_id']:
                conn.rollback()
                return
            assert_accepted_epoch(conn,request)
            if source.get('supplier_input_snapshot') and _supplier_input_snapshot(conn,request['source_id']) != source['supplier_input_snapshot']:
                from packages.application.ff_pool_documents import FfPoolDocumentError
                raise FfPoolDocumentError('supplier_source_revision_changed','Поставка или её стоимость изменились после подтверждения')
            plan = _build_posting_plan(conn, request=request, manifest=source['manifest'], epoch=_writer_epoch(conn))
            assert_effect(conn, request, plan)
            assert_reservations(conn, plan=plan, epoch=request['idempotency_epoch'])
            preview = dict(source['manifest'])
            if _is_guided_china_request(request):
                preview['posting_plan_preview'] = _posting_plan_preview(plan=plan,epoch=request['idempotency_epoch'])
            conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='ready',preview_manifest_json=?,error_code='',error_details_json='null' WHERE request_id=? AND posted_document_id=''", (json.dumps(preview,ensure_ascii=False),row['request_id']))
            conn.commit()
        service._post_once_under_writer_lock(row['request_id'])


def try_post(db_path, runtime_dir, identity, *, timestamp_factory=None, owned_cycle=False):
    """Bounded immediate physical attempt; saved intent survives every later error."""
    from packages.application.ff_pool_documents import FfPoolDocumentService
    from packages.application.warehouse_functional_lock import warehouse_functional_job_is_busy, require_warehouse_job_owner
    if owned_cycle:
        require_warehouse_job_owner(runtime_dir)
    elif warehouse_functional_job_is_busy(runtime_dir):
        _update(db_path,identity,'delayed','warehouse_busy')
        return
    with (Path(runtime_dir)/'.ff-pool-document-posting.lock').open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            _update(db_path,identity,'delayed','warehouse_busy')
            return
        try:
            service = FfPoolDocumentService(db_path=Path(db_path),runtime_dir=Path(runtime_dir),resume=False,bootstrap=False,timestamp_factory=timestamp_factory)
            _post(service,{'request_id':identity})
            _update(db_path,identity,'processing','publication_pending')
        except Exception as exc:
            code = getattr(exc,'code','') or type(exc).__name__
            permanent = code in {'operator_source_changed','operator_authorized_effect_changed','reserved_stock_unavailable','supplier_source_revision_changed','feature_epoch_changed','supplier_shipment_already_accepted','transfer_outcome_exceeds_open','transfer_shipment_exists','insufficient_source_balance','inventory_source_prestate_changed','inventory_incomplete_active_roster','storno_exists','negative_pool_balance','pool_quantity_capital_zero_mismatch'}
            permanent=permanent or (code.startswith('guided_recovery_') and code.endswith('_drift'))
            _update(db_path,identity,'needs_attention' if permanent else 'delayed',code)
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def drain(runtime, *, limit=100, timestamp_factory=None):
    from packages.application.ff_pool_documents import FfPoolDocumentService, _connect, REQUESTS_TABLE, _is_guided_china_request
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    require_warehouse_job_owner(runtime.runtime_dir)
    with closing(readonly(runtime.db_path)) as conn:
        rows = conn.execute(f"SELECT * FROM {TABLE} WHERE state IN ('accepted','processing','delayed') ORDER BY accepted_at,request_id LIMIT ?",(limit,)).fetchall() if exists(conn) else []
    service = FfPoolDocumentService(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,resume=False,bootstrap=False,timestamp_factory=timestamp_factory)
    for row in rows:
        identity=row['request_id']
        try_post(runtime.db_path,runtime.runtime_dir,identity,timestamp_factory=timestamp_factory,owned_cycle=True)
        with closing(_connect(runtime.db_path,query_only=True)) as conn:
            request=conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",(identity,)).fetchone()
        if request['posted_document_id'] and (_is_guided_china_request(request) or service._guided_recovery_target(request)):
            try:
                if _is_guided_china_request(request):
                    with closing(readonly(runtime.db_path)) as conn:
                        compensated=conn.execute('SELECT 1 FROM sheet_vitrina_v1_ff_guided_acceptance_recoveries WHERE target_request_id=?',(identity,)).fetchone()
                    if not compensated:
                        service._replay_guided_acceptance(request)
                else:
                    service._replay_guided_recovery(request)
            except Exception as exc:
                _update(runtime.db_path,identity,'delayed',getattr(exc,'code','') or type(exc).__name__)
    return {'processed_count':len(rows),'request_ids':[r['request_id'] for r in rows]}


def may_finalize(db_path, runtime_dir, identity):
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f"SELECT source_digest,receipt_json FROM {TABLE} WHERE request_id=?",(identity,)).fetchone() if exists(conn) else None
    if row is None:
        return True
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    try:
        require_warehouse_job_owner(runtime_dir)
    except RuntimeError:
        return False
    receipt=json.loads(row['receipt_json'])
    return receipt.get('publication_verified') is True and receipt.get('source_digest') == row['source_digest']


def record_functional_publication(conn, *, request, version_id, plan_fingerprint):
    """Called only inside the actual native functional publication transaction."""
    if not exists(conn):
        return
    if request['stable_source_id'].startswith('ff_pool_document:'):
        doc_id = request['stable_source_id'].split(':',1)[1]
        rows = conn.execute(f"SELECT c.* FROM {TABLE} c JOIN sheet_vitrina_v1_ff_pool_documents d USING(request_id) WHERE d.document_id=?",(doc_id,)).fetchall()
        key='functional_publication'
    elif request['stable_source_id'].startswith('supplier_shipment:'):
        rows=conn.execute(f"SELECT * FROM {TABLE} WHERE json_extract(source_json,'$.native_recovery.shipment_id')=? AND json_extract(source_json,'$.request_identity')=?",(request['stable_source_id'].split(':',1)[1],request['source_revision'])).fetchall()
        key='recovery_functional_publication'
        doc_id=''
    else:
        return
    from packages.application.warehouse_functional import _targeted_recalc_request_identity
    for row in rows:
        receipt = json.loads(row['receipt_json'])
        receipt[key] = _targeted_recalc_request_identity(request)
        receipt[key].update(version_id=version_id,plan_fingerprint=plan_fingerprint,document_id=doc_id)
        conn.execute(f"UPDATE {TABLE} SET receipt_json=? WHERE request_id=?",(json.dumps(receipt,ensure_ascii=False),row['request_id']))


def reconcile(runtime, *, request_ids, finance_receipt, economics_receipt=None, now=None):
    """Acknowledge only actual exact accounting/ready/Finance and native readback."""
    from packages.application.ff_pool_documents import FfPoolDocumentService, REQUESTS_TABLE, _fingerprint
    from packages.application.fbs_accounting_runtime import load, current_publication_receipt
    from packages.application.fbs_snapshot_cost_sources import _documents
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    require_warehouse_job_owner(runtime.runtime_dir)
    book, book_version = load(runtime.runtime_dir)
    try:
        publication = current_publication_receipt(runtime,now=now)
    except (ValueError,sqlite3.Error):
        publication = None
    selected = set(request_ids)
    with closing(readonly(runtime.db_path)) as conn:
        rows = [r for r in conn.execute(f"SELECT * FROM {TABLE} WHERE state IN ('accepted','processing','delayed')") if r['request_id'] in selected] if exists(conn) else []
        documents = {d['document_id']:d for d in _documents(conn)} if rows else {}
    economics_receipt=(economics_receipt or {}).get('accounting_publication') or economics_receipt
    completed=0
    for row in rows:
        identity=row['request_id']
        source=json.loads(row['source_json'])
        receipt=json.loads(row['receipt_json'])
        historical=(book or {}).get('historical_revision',{})
        if source['business_date'] < max((book or {}).get('state',{}).get('periods',{'':{}})) and any(
                d['document_id'] in historical.get('receipt_document_ids',[]) for d in source['effect']['documents']):
            from packages.application.fbs_accounting_historical_cycle import completion_authorized
            with closing(readonly(runtime.db_path)) as conn:
                if not completion_authorized(conn,identity):continue
        owned=[d for d in documents.values() if d['document_id'] in {d['document_id'] for d in source['effect']['documents']}]
        if len(owned) != len(source['effect']['documents']):
            continue
        if source['document_kind'] != 'transfer_root':
            pending = next((p for p in (book or {}).get('state',{}).get('pending_documents',[]) if p.get('document_id') in {d['document_id'] for d in owned}),None)
            if pending:
                _update(runtime.db_path,identity,'needs_attention' if pending['reason']=='late_closed_period_document' else 'delayed',pending['reason'])
                continue
            consumed=dict((book or {}).get('state',{}).get('baseline',{}).get('absorbed_documents',{}))
            for period in (book or {}).get('state',{}).get('periods',{}).values():
                consumed.update(period.get('applied_documents',{}))
            if (not publication or publication['accounting_version']!=book_version
                    or any(consumed.get(d['document_id'])!=d['fingerprint'] for d in owned)):
                continue
            functional=receipt.get('functional_publication',{})
            primary=source['effect']['primary_document_id']
            expected_revision=_fingerprint({'request_identity':source['request_identity'],'effect':source['effect'],'business_date':source['business_date']})
            expected_nm=affected_nm_ids(source['effect'])
            if (functional.get('stable_source_id')!='ff_pool_document:'+primary
                    or functional.get('source_revision')!=expected_revision
                    or functional.get('effective_date')!=source['business_date']
                    or functional.get('affected_nm_ids')!=expected_nm):
                continue
            with closing(readonly(runtime.db_path)) as conn:
                queue=conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE queue_id=? AND stable_source_id=? AND source_revision=?",(functional.get('queue_id',''),functional.get('stable_source_id',''),functional.get('source_revision',''))).fetchone()
                version=conn.execute("SELECT plan_fingerprint,status FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",(functional.get('version_id',''),)).fetchone()
            if (not queue or queue['status']!='complete' or not version or version['status']!='good'
                    or version['plan_fingerprint']!=functional.get('plan_fingerprint')):
                continue
            recovery=source.get('native_recovery') or {}
            if recovery:
                extra=receipt.get('recovery_functional_publication',{})
                if (extra.get('stable_source_id')!='supplier_shipment:'+recovery['shipment_id']
                        or extra.get('source_revision')!=source['request_identity']
                        or extra.get('effective_date')!=source['business_date']
                        or extra.get('affected_nm_ids')!=sorted(recovery['affected_nm_ids'])):
                    continue
                with closing(readonly(runtime.db_path)) as conn:
                    extra_queue=conn.execute("SELECT status FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE queue_id=? AND stable_source_id=? AND source_revision=?",(extra.get('queue_id',''),extra['stable_source_id'],extra['source_revision'])).fetchone()
                    extra_version=conn.execute("SELECT status,plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",(extra.get('version_id',''),)).fetchone()
                if (not extra_queue or extra_queue['status']!='complete' or not extra_version
                        or extra_version['status']!='good' or extra_version['plan_fingerprint']!=extra.get('plan_fingerprint')):
                    continue
            if (finance_receipt.get('status') not in {'applied','already_current'}
                    or finance_receipt.get('accounting_version')!=book_version
                    or finance_receipt.get('accounting_version_before')!=book_version
                    or finance_receipt.get('accounting_version_unchanged') is not True
                    or not finance_receipt.get('fingerprint','').startswith('sha256:')
                    or finance_receipt.get('non_target_preserved') is not True
                    or finance_receipt.get('source_advanced_after_apply') is True
                    or type(finance_receipt.get('post_verify_stale_week_count')) is not int
                    or finance_receipt['post_verify_stale_week_count']!=0):
                continue
            if (not economics_receipt or economics_receipt.get('accounting_version')!=book_version
                    or economics_receipt.get('ready_digest')!=publication['ready_digest']):
                continue
            receipt.update(economics_publication=economics_receipt,accounting_publication=publication,document_operands={d['document_id']:d['fingerprint'] for d in owned},
                           finance_publication={k:finance_receipt.get(k) for k in ('status','fingerprint','source_dependency','post_source_dependency','target_image_digest','non_target_preserved')})
        receipt.update(publication_verified=True,source_digest=row['source_digest'])
        _update(runtime.db_path,identity,'processing','publication_pending',receipt)
        service=FfPoolDocumentService(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,resume=False,bootstrap=False)
        try:
            with (Path(runtime.runtime_dir)/'.ff-pool-document-posting.lock').open('a+b') as handle:
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                with warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=2):
                    service._finalize_posted(identity)
                    readback=service._verify_posted_readback(identity)
                    with closing(readonly(runtime.db_path)) as conn:
                        native=conn.execute(f"SELECT r.state,recovery.lifecycle_state,recovery.after_digest FROM {REQUESTS_TABLE} r JOIN sheet_vitrina_v1_recovery_operations recovery ON recovery.operation_id=r.recovery_operation_id WHERE r.request_id=?",(identity,)).fetchone()
                    if not native or native['state']!='complete' or native['lifecycle_state']!='retained' or native['after_digest']!=_fingerprint(readback):
                        raise ValueError('operator_native_not_terminal')
                receipt.update(native_readback=readback,native_recovery=dict(native))
                _update(runtime.db_path,identity,'completed','',receipt)
                completed+=1
        except Exception as exc:
            _update(runtime.db_path,identity,'delayed',getattr(exc,'code','') or type(exc).__name__)
    return {'processed_count':completed}
