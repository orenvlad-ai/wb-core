"""Heavy query-only supplier source -> native cost/book/ready/Finance proof.

The caller retains live RO observers through its short completion CAS. This
module neither bootstraps schemas nor submits, enqueues, or publishes anything.
"""
from contextlib import closing, nullcontext, ExitStack
from datetime import date
from decimal import Decimal
import json
import re
import sqlite3
from copy import deepcopy
from packages.application import operator_supplier_shipments as source
from packages.application import operator_supplier_processing as processing


def numerical_supplier_state(conn, shipment_id):
    """Use the exact native allocator on its real complete SQL operands."""
    from packages.application.warehouse_functional import _supplier_cost_allocations
    queries = {
        "shipments": ("supplier_shipments", "shipment_id", "shipment_id"),
        "shipment_lines": ("supplier_shipment_lines", "shipment_id", "shipment_id,sort_order,line_id"),
        "cny_operations": ("cny_ledger_operations", "source_order_id", "sequence_key,operation_id"),
        "cny_documents": ("cny_documents", "source_order_id", "operation_date,operation_datetime,document_id"),
        "financial_documents": ("supplier_financial_documents", "supplier_order_id", "document_date,document_id"),
        "financial_expense_lines": ("supplier_financial_expense_lines", "supplier_order_id", "supplier_order_id,financial_document_id,sort_order,line_id"),
        "bank_operation_assignments": ("supplier_bank_operation_assignments", "supplier_order_id", "supplier_order_id,semantic_operation_id"),
        "payment_fee_confirmations": ("supplier_payment_fee_confirmations", "supplier_order_id", "supplier_order_id,payment_document_id,confirmation_id"),
    }
    operands = {key: [dict(row) for row in conn.execute(f"SELECT * FROM sheet_vitrina_v1_{table} WHERE {field}=? ORDER BY {order}", (shipment_id,))]
                for key, (table, field, order) in queries.items()}
    allocation = _supplier_cost_allocations(operands).get(shipment_id)
    state = {"shipment_id": shipment_id, "source_fingerprint": allocation["source_fingerprint"],
             "calculation_fingerprint": allocation["calculation_fingerprint"],
             "expenses_complete": int(allocation["expenses_complete"]),
             "calculation_available": int(not allocation["blockers"])} if allocation else None
    return state, allocation


def validate_wb_projection(conn, queue_ref):
    """Rebuild expected persisted layer payloads using native pure functions.

    Every goods row participates in the full supply denominator and closure,
    including SKUs absent from the active catalogue. No UI projection/filter or
    queue flag is authority for quantities, prices, or dates.
    """
    from math import isclose
    from packages.application import our_wb_costs as native
    from packages.application.fulfillment_services import FulfillmentServicesBlock
    from packages.application.registry_upload_db_backed_runtime import _wb_supply_transit_cost_enrichment_to_dict
    ids = set(queue_ref["affected_nm_ids"])
    current_ff = {}
    for row in conn.execute("SELECT line.*,layer.status layer_status,layer.accepted_ff_date,layer.weighted_avg_ff_unit_cost_rub,layer.component_status_json layer_component_status_json FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines line JOIN sheet_vitrina_v1_supplier_ff_cost_layers layer ON layer.layer_id=line.layer_id WHERE layer.is_current=1 AND line.nm_id IS NOT NULL ORDER BY layer.accepted_ff_date DESC,layer.calculated_at DESC"):
        current_ff.setdefault(row["nm_id"], dict(row))
    overlays = FulfillmentServicesBlock.approved_overlay_in_connection(conn)
    enrichments = {}
    for row in conn.execute("SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment ORDER BY updated_at DESC,supply_id DESC"):
        item = _wb_supply_transit_cost_enrichment_to_dict(row)
        if native._canonical_seller_portal_transit_enrichment(item):
            enrichments[str(item["supply_id"])] = item
    block = native.OurWbCostBlock(runtime=None)  # Pure payload builder; no runtime reader.
    for row in conn.execute("SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE COALESCE(supply_date,fact_date,updated_date,'')>=? ORDER BY supply_id", (native.OUR_WB_COST_OPENING_DATE,)):
        supply = native._wb_supply_row_to_dict(row)
        goods = native._parse_wb_goods(supply.get("raw_goods_json"))
        if not any(int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0) in ids for item in goods):
            continue
        enrichment = enrichments.get(str(supply["supply_id"]))
        if enrichment:
            supply.update(seller_portal_transit_cost=enrichment.get("amount"), effective_transit_cost_total=enrichment.get("amount"),
                effective_transit_cost_source=(str(enrichment.get("source") or "")+":"+str(enrichment.get("evidence_type") or "")).strip(":"))
        denominator = native._sum_positive(native._wb_good_packed_quantity(item).qty for item in goods)
        expected, overlay = {}, overlays.get(str(supply["supply_id"]))
        normalized = native._normalized_wb_row(supply)
        transit = native.classify_wb_supply_transit(normalized, denominator=denominator)
        acceptance = native._number_or_zero(normalized.get("acceptance_cost") if normalized.get("acceptance_cost") is not None else normalized.get("acceptanceCost"))
        accepted_denominator = native._sum_positive(native._number_or_zero(item.get("acceptedQuantity") if item.get("acceptedQuantity") is not None else item.get("accepted_quantity")) for item in goods)
        services = native._number_or_zero((overlay or {}).get("service_amount_with_vat_without_storage_total"))
        storage = native._number_or_zero((overlay or {}).get("storage_allocated_amount_with_vat_total"))
        for item in goods:
            nm = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
            packed, accepted = native._wb_good_packed_quantity(item), native._wb_good_accepted_quantity(item)
            try:
                value = next(item[key] for key in ("quantity", "qty") if item.get(key) is not None)
                if nm <= 0 or not Decimal(str(value)).is_finite() or Decimal(str(value)) < 0:
                    raise ValueError()
            except (ValueError, ArithmeticError, StopIteration):
                raise ValueError("supplier_wb_supply_authority_incomplete") from None
            if packed.qty <= 0:
                continue
            if denominator <= 0:
                raise ValueError("supplier_wb_supply_authority_incomplete")
            expected[nm] = block._build_wb_supply_cost_layer_payload(supply=supply, nm_id=nm,
                accepted_qty=accepted.qty, quantity_source=accepted.source,
                quantity_is_final_accepted=native._wb_good_quantity_is_final_accepted(supply=supply, quantity=accepted),
                denominator=denominator, ff_line=current_ff.get(nm), transit=transit,
                services_total=services, services_per_unit=services/denominator,
                storage_total=storage, storage_per_unit=storage/denominator,
                acceptance_total=acceptance, acceptance_per_accepted_unit=acceptance/accepted_denominator if accepted_denominator > 0 else 0,
                overlay=overlay)
        actual = {r["nm_id"]: dict(r) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? AND is_current=1", (str(supply["supply_id"]),))}
        if set(actual) != set(expected):
            raise ValueError("supplier_wb_supply_sku_scope_changed")
        for nm, payload in expected.items():
            layer = actual[nm]
            if layer["inputs_hash"] != native._stable_hash(payload["input"]) or json.loads(layer["component_status_json"]) != payload["component_status"]:
                raise ValueError("supplier_wb_cost_projection_pending")
            for key, value in payload.items():
                if key in {"input", "component_status"}:
                    continue
                saved = layer[key]
                if isinstance(value, (float, int)):
                    same = saved is not None and isclose(float(saved), float(value), rel_tol=0, abs_tol=1e-8)
                else:
                    same = saved == value
                if not same:
                    raise ValueError("supplier_wb_cost_projection_pending")

def _finance_business_image(images):
    result = {}
    for table, image in images.items():
        values = []
        for row in image['rows']:
            item = dict(zip(image['columns'], row))
            for key in ('calculated_at', 'checked_at'):
                item.pop(key, None)  # Native calculation clocks, not operands.
            values.append({key: json.loads(value) if key.endswith('_json') and value else value
                           for key, value in item.items()})
        if values:
            result[table] = sorted(values, key=source._json)
    return result


def _finance_retained_stamp_no_change(current, expected, *, shared_version):
    """Native economic no-change can retain ONLY these two global stamps.

    Per-date source IDs/digests, signatures, money, quality and all unknown
    fields still compare exactly. No Finance row is rewritten by this proof.
    """
    from packages.application.shared_sku_cost import POLICY
    def version(value):
        if not isinstance(value, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None:
            raise ValueError('supplier_finance_global_stamp_unproven')
        return value
    version(shared_version)
    def normalize(image, *, native):
        image = deepcopy(image); stamps = {}
        for table, rows in image.items():
            for row in rows:
                key = source._json({k: row[k] for k in ('seller_id','week_start','week_end','nm_id') if k in row})
                metadata = []
                for column, path in (('quality_json', ('shared_cost',)), ('coverage_json', ('quality','shared_cost'))):
                    value = row.get(column)
                    if not value:
                        continue
                    if not isinstance(value, dict):
                        raise ValueError('supplier_finance_global_stamp_unproven')
                    node = value
                    for field in path:
                        if field not in node:
                            node = None; break
                        node = node[field]
                        if not isinstance(node, dict):
                            raise ValueError('supplier_finance_global_stamp_unproven')
                    if node is None:
                        continue
                    if (set(node) != {'cost_method_version','policy_date','effective_date','version_id','candidate_only'}
                            or node['cost_method_version'] != POLICY or node['candidate_only'] is not False):
                        raise ValueError('supplier_finance_global_stamp_unproven')
                    for field in ('policy_date','effective_date'):
                        try:
                            if date.fromisoformat(node[field]).isoformat() != node[field]:
                                raise ValueError('invalid date')
                        except (ValueError, TypeError):
                            raise ValueError('supplier_finance_global_stamp_unproven') from None
                    saved_version = version(node['version_id'])
                    if native and saved_version != shared_version:
                        raise ValueError('supplier_finance_global_stamp_unproven')
                    identity = (table, key, column+'.'+'.'.join(path)+'.version_id')
                    if identity in stamps:
                        raise ValueError('supplier_finance_global_stamp_unproven')
                    stamps[identity] = saved_version; metadata.append(deepcopy(node))
                    node['version_id'] = shared_version
                # Both native copies must exist and agree when both columns
                # are present; a partial/foreign quality shape is not lineage.
                if ('quality_json' in row and 'coverage_json' in row and metadata
                        and (len(metadata) != 2 or metadata[0] != metadata[1])):
                    raise ValueError('supplier_finance_global_stamp_unproven')
            image[table] = sorted(rows, key=source._json)
        return image, stamps
    actual_image, actual_stamps = normalize(current, native=False)
    expected_image, expected_stamps = normalize(expected, native=True)
    if actual_image != expected_image or set(actual_stamps) != set(expected_stamps):
        raise ValueError('supplier_finance_target_projection_pending')
    differences = [{'table':table, 'row_key':json.loads(key), 'path':path,
                    'retained_finance_version_id':actual_stamps[(table,key,path)],
                    'current_native_shared_version_id':expected_stamps[(table,key,path)]}
                   for table,key,path in sorted(actual_stamps)
                   if actual_stamps[(table,key,path)] != expected_stamps[(table,key,path)]]
    if not differences:
        raise ValueError('supplier_finance_target_projection_pending')
    return differences


def _finance_snapshot(block, conn, *, seller_id, queue_ref, shared_version, projections=None):
    from packages.application.wb_finance_weekly import _nomenclature_identity_index, _resolve_finance_nm_id, _operation_date
    aliases, ambiguous, _, _ = _nomenclature_identity_index(conn)
    targets = set()
    scope_ids = {str(nm) for nm in queue_ref["affected_nm_ids"]}
    effective = date.fromisoformat(queue_ref["effective_date"])
    rows = conn.execute("SELECT week_start,week_end,raw_json FROM wb_finance_weekly_raw_rows "
                        "WHERE seller_id=? AND week_end>=? ORDER BY week_start,rrd_id",
                        (seller_id, effective.isoformat()))
    for row in rows:
        operation = json.loads(row["raw_json"])
        nm, _, problem = _resolve_finance_nm_id(operation, alias_to_nm=aliases, ambiguous_aliases=ambiguous)
        if (problem and str(operation.get("docTypeName") or "").casefold() in {"продажа", "возврат"}
                and Decimal(str(operation.get("quantity") or 0)) != 0):
            raise ValueError("supplier_finance_scope_unknown")
        if problem or str(nm) not in scope_ids:
            continue
        day, day_source = _operation_date(operation, date.fromisoformat(row["week_start"]))
        if day_source == "week_start_fallback":
            raise ValueError("supplier_finance_operation_date_unknown")
        if day >= effective:
            targets.add((seller_id, row["week_start"], row["week_end"]))
    if shared_version and (block.shared_cost_snapshot is None
            or block.shared_cost_snapshot.metadata()["version_id"] != shared_version):
        raise ValueError("supplier_finance_accounting_version_mismatch")
    dependency = block._finance_source_dependency_fingerprint(conn, target_keys=targets, force_reload=True)
    current = block._finance_target_images(conn, targets)
    expected = {}
    for _, start, end in sorted(targets):
        key = (shared_version, start, end)
        projection = projections.get(key) if projections is not None else None
        if projection is None:
            projection = block._build_week_target_projection(conn, week_start=date.fromisoformat(start), week_end=date.fromisoformat(end))
            if projections is not None:projections[key] = projection
        if projection["coverage"]["unmatched_units"]:
            raise ValueError("supplier_finance_cost_incomplete")
        for table, image in projection["images"].items():
            expected.setdefault(table, {"columns": image["columns"], "rows": []})["rows"].extend(image["rows"])
    expected = block._canonicalize_finance_target_images(conn, expected)
    current_image = _finance_business_image(current)
    expected_image = _finance_business_image(expected)
    stamp_differences = []
    if current_image != expected_image:
        stamp_differences = _finance_retained_stamp_no_change(current_image, expected_image, shared_version=shared_version)
    result = {"status": "verified" if targets else "not_applicable", "seller_id": seller_id,
            "target_weeks": [list(key) for key in sorted(targets)],
            "source_dependency": dependency, "target_digest": source.digest(current_image),
            "non_target_digest": block._finance_state_digest(conn, target_keys=targets, target_only=False)}
    if stamp_differences:
        result.update(projection_outcome='derived_no_change', retained_global_stamp_differences=stamp_differences,
                      current_native_expected_target_digest=source.digest(expected_image))
    return result


class FinanceProofCohort:
    """One bounded reconcile preparation; never reuse across receipt commits."""
    def __init__(self, runtime, *, seller_id, stack):
        self.runtime, self.seller_id, self.stack = runtime, seller_id, stack
        self.block = self.conn = None
        self.results, self.projections = {}, {}
        self.sealed = False
        self.open_error = None

    def _path_identity(self):
        try:
            return tuple((str(path), path.stat().st_dev, path.stat().st_ino) for path in self.paths)
        except OSError as exc:
            raise ValueError('supplier_finance_cohort_authority_changed') from exc

    def _authority(self):
        return self.block.store_registry.load(), self._path_identity()

    def _observe(self, conn):
        return self.block._sqlite_data_version_token(conn), self._authority()

    def _open(self):
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        self.block = WbFinanceWeeklyBlock(self.runtime.runtime_dir, seller_id=self.seller_id)
        manifest = self.block.store_registry.load()
        from pathlib import Path
        if self.block.store_registry.resolve('operational', manifest=manifest) != Path(self.runtime.db_path).resolve():
            raise ValueError('supplier_finance_cohort_authority_changed')
        self.paths = sorted({self.block.store_registry.resolve(role, manifest=manifest) for role in ('operational', 'finance_raw')})
        identity = self._path_identity()
        authority = []
        # Opening failures have no shared successful snapshot to retain. Do
        # not leave a partially opened reader in the enclosing writer cohort.
        with ExitStack() as opening:
            conn = opening.enter_context(closing(self.block._connect_stale_cost_plan(storage_authority=authority)))
            self.block._assert_readonly_plan_connection(conn)
            if authority != [manifest] or self._authority() != (manifest, identity):
                raise ValueError('supplier_finance_cohort_authority_changed')
            expected = self._observe(conn)
            conn.execute('BEGIN')
            self.conn, self.expected = conn, expected
            self.stack.enter_context(opening.pop_all())

    def read(self, *, queue_ref, shared_version, handoff):
        if self.sealed:raise ValueError('supplier_finance_cohort_already_sealed')
        if self.open_error is not None:raise self.open_error
        if self.conn is None:
            try:self._open()
            except (ValueError, sqlite3.OperationalError) as exc:
                self.open_error = exc
                raise
        # The full native connection, including its capitalization cache, is
        # pinned for this preparation. A scope key never crosses a commit.
        key = (tuple(sorted({str(nm) for nm in queue_ref['affected_nm_ids']})), queue_ref['effective_date'], shared_version)
        if key not in self.results:
            self.results[key] = _finance_snapshot(self.block, self.conn, seller_id=self.seller_id,
                queue_ref=queue_ref, shared_version=shared_version, projections=self.projections)
        if handoff is not None:handoff.append((self.conn, self._observe, self.expected))
        return deepcopy(self.results[key])

    def check_independent(self):
        # The main operational store is now locked by its own writer. Do not
        # read that other main observer after receipt DML/cache spill.
        from pathlib import Path
        operational = Path(self.runtime.db_path).resolve()
        if self._authority() != self.expected[1]:
            raise ValueError('supplier_finance_cohort_authority_changed')
        for row in self.conn.execute('PRAGMA database_list'):
            schema = str(row[1])
            if schema != 'temp' and Path(row[2]).resolve() != operational:
                if self.conn.execute('PRAGMA '+schema+'.data_version').fetchone()[0] != self.expected[0][schema]:
                    raise ValueError('supplier_completion_handoff_changed')

    def seal(self):
        if self.open_error is not None:raise self.open_error
        if self.conn is not None:
            self.conn.commit()
            if self._observe(self.conn) != self.expected:
                raise ValueError('supplier_finance_read_snapshot_changed')
        self.sealed = True


def _finance(runtime, *, seller_id, queue_ref, shared_version, handoff=None, stack=None, cohort=None):
    if cohort is not None:
        return cohort.read(queue_ref=queue_ref, shared_version=shared_version, handoff=handoff)
    from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
    block = WbFinanceWeeklyBlock(runtime.runtime_dir, seller_id=seller_id)
    owner = stack.enter_context(closing(block._connect_stale_cost_plan())) if stack is not None else None
    with nullcontext(owner) if owner is not None else closing(block._connect_stale_cost_plan()) as conn:
        block._assert_readonly_plan_connection(conn)
        before = block._sqlite_data_version_token(conn)
        conn.execute('BEGIN')
        result = _finance_snapshot(block, conn, seller_id=seller_id, queue_ref=queue_ref, shared_version=shared_version)
        conn.commit()
        if block._sqlite_data_version_token(conn) != before:
            raise ValueError('supplier_finance_read_snapshot_changed')
        if handoff is not None:handoff.append((conn, block._sqlite_data_version_token, before))
        return result



def read_correlated_native(conn, operation_id):
    """Exact native source/queue/cost publisher proof, without a book fallback.

    The supplier historical owner uses this same numerical proof. This is not
    completion; dated book/ready/History/Finance remain separate consumers.
    """
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("supplier_proof_query_only_required")
    op = processing.operation(conn, operation_id)
    if not op["intent"] or not processing.current(conn, op):
        raise ValueError("supplier_completion_source_changed")
    queue = processing.queue_for(conn, op)
    saved = conn.execute(f"SELECT proof.* FROM {processing.FUNCTIONAL} proof JOIN sheet_vitrina_v1_warehouse_functional_versions version USING(version_id) WHERE proof.operation_id=? ORDER BY version.published_at DESC,version.created_at DESC,version.version_id DESC LIMIT 1", (operation_id,)).fetchone()
    if not saved or source.digest(json.loads(saved["proof_json"])) != saved["proof_digest"]:
        raise ValueError("supplier_correlated_functional_proof_missing")
    functional = json.loads(saved["proof_json"])
    if functional["source_ref"] != processing.ref(op) or functional["queue_ref"] != processing.queue_ref(queue):
        raise ValueError("supplier_correlated_source_changed")
    version = conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?", (functional["version_id"],)).fetchone()
    if not version or version["status"] != "good" or version["plan_fingerprint"] != functional["plan_fingerprint"]:
        raise ValueError("supplier_functional_version_missing")
    # Current rematerialized layers cannot stand in for an older native
    # publication. Bind the actual publisher's CAS-protected source and
    # numerical inputs, then independently verify their present authority.
    from packages.application.warehouse_functional import _current_wb_supply_revisions, _DOWNSTREAM_COST_ROWS_SQL, _hash
    if functional.get("wb_supply_source_digest") != "sha256:" + _hash(_current_wb_supply_revisions(conn)):
        raise ValueError("supplier_native_wb_source_changed")
    cost_rows = [dict(row) for row in conn.execute(_DOWNSTREAM_COST_ROWS_SQL)]
    if functional.get("cost_projection_digest") != "sha256:" + _hash(cost_rows):
        raise ValueError("supplier_native_cost_projection_changed")
    state, allocation = numerical_supplier_state(conn, op["shipment_id"])
    if state != functional["cost_state"] or state != processing.cost_state(conn, version["version_id"], op["shipment_id"]):
        raise ValueError("supplier_native_cost_operands_changed")
    if allocation and allocation["blockers"]:
        raise ValueError("supplier_native_cost_unavailable")
    from packages.application.warehouse_functional import _supplier_allocation_with_certification
    certification = _supplier_allocation_with_certification(allocation, active_version_id=version["version_id"],
        active_fingerprints=(state["source_fingerprint"], state["calculation_fingerprint"])
            if state and state["expenses_complete"] and state["calculation_available"] else None)["certification"] if allocation else None
    if processing.balance_rows(conn, version["version_id"], functional["queue_ref"]["affected_nm_ids"]) != functional["balance_rows"]:
        raise ValueError("supplier_native_balance_publication_changed")
    validate_wb_projection(conn, functional["queue_ref"])
    return {"operation": op, "queue": queue, "functional": functional, "version": dict(version),
            "state": state, "allocation": allocation, "certification": certification}


def read_native_proof(runtime, operation_id, *, seller_id="canonical", now=None, connection=None, handoff=None, stack=None, finance_cohort=None):
    from packages.application.shared_sku_cost_sources import capture_wb_component
    from packages.application import fbs_accounting_runtime as accounting, ready_publication
    owner = nullcontext(connection) if connection is not None else closing(source.readonly(runtime.db_path))
    with owner as conn:
        if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ValueError("supplier_proof_query_only_required")
        token = lambda c: int(c.execute("PRAGMA main.data_version").fetchone()[0])
        before = token(conn)
        native = read_correlated_native(conn, operation_id)
        op, queue, functional, version = (native[key] for key in ("operation", "queue", "functional", "version"))
        state, allocation, certification = (native[key] for key in ("state", "allocation", "certification"))
        day = functional["business_date"]
        ids = functional["queue_ref"]["affected_nm_ids"]
        daily = [dict(item) for item in conn.execute(
            "SELECT as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,fingerprint "
            "FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=? AND as_of_date>=? AND as_of_date<=? "
            "AND nm_id IN ("+','.join('?' for _ in ids)+") ORDER BY as_of_date,nm_id",
            ('warehouse_functional_cutover_v1',functional["queue_ref"]["effective_date"],day,*ids))]
        if daily != functional["daily_cost_rows"]:
            raise ValueError("supplier_dated_cost_publication_changed")
        wb = capture_wb_component(runtime.db_path, day=day, nm_ids=functional["queue_ref"]["affected_nm_ids"],
                                  version_id=version["version_id"], connection=conn)
        if not wb["complete"] or not wb["authority_complete"]:
            raise ValueError("supplier_exact_wb_authority_incomplete:" + wb["reason"])
        book, book_version = accounting.load(runtime.runtime_dir)
        if not book or not book["active"]:
            raise ValueError("supplier_accounting_not_active")
        bound = book["wb_days"].get(day)
        if (not bound or not bound["complete"] or bound["version_id"] != version["version_id"]
                or bound["source_digest"] != capture_wb_component(runtime.db_path, day=day,
                    nm_ids=bound["requested_nm_ids"], version_id=version["version_id"], connection=conn)["source_digest"]):
            raise ValueError("supplier_accounting_exact_version_pending")
        from packages.application import operator_supplier_history_candidate as candidate
        from datetime import datetime, timezone
        evaluation=candidate.prepare(runtime,operation_id,now=now or datetime.now(timezone.utc)) if functional.get('origin_before_version_id') and op['action']!='factual_date' else None
        # A current green book cannot stand in for a changed closed historical day.
        # Current continuation is supported; an older changed cost needs its own
        # admitted dated authority/book revision from the closed-history owner.
        for item in ([] if evaluation and evaluation.get('structural_no_effect') else daily):
            prior_day = item["as_of_date"]
            if prior_day < day and Decimal(item["quantity"]) > 0:
                prior = book["wb_days"].get(prior_day)
                if not prior or not prior["complete"]:
                    raise ValueError("supplier_historical_authority_required:"+prior_day)
                operand = next((r for r in prior["rows"] if r["nm_id"] == item["nm_id"]), None)
                if (not operand or operand["status"] != "available" or Decimal(operand["quantity"]) <= 0
                        or Decimal(operand["capital_rub"])/Decimal(operand["quantity"]) != Decimal(item["wac_rub"])):
                    raise ValueError("supplier_historical_publication_required:"+prior_day)
        from packages.application import operator_supplier_history as history
        history_row=history.publication_for(conn,processing.ref(op))
        dated_history=history.completed_proof(conn,processing.ref(op)) if history_row else None
        if history_row and not dated_history:
            raise ValueError('supplier_history_exact_ack_pending')
        if dated_history:
            historical_receipt=history.SupplierHistory(runtime,dated_history['manifest']['operation_id'],dated_history['manifest']['attempt_id'])
            historical_receipt.validate_sources_readonly(now=now)
        economics = accounting.current_publication_receipt(runtime, now=now)
        if not economics or economics["accounting_version"] != book_version or economics["business_date"] != day:
            raise ValueError("supplier_accounting_ready_pending")
        published = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",
                                 (economics["operation_id"], economics["attempt_id"])).fetchone()
        ready = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                             (published["bundle_version"], published["as_of_date"])).fetchone() if published else None
        if (not published or published["state"] != "complete" or not ready
                or published["book_version"] != book_version or ready_publication.digest(ready[0]) != published["after_digest"]):
            raise ValueError("supplier_dated_ready_receipt_mismatch")
        shared_version = accounting.ActiveSharedCostSnapshot(list(book["shared_days"].values()),
            effective_date=book["effective_date"]).metadata()["version_id"]
        finance = _finance(runtime, seller_id=seller_id, queue_ref=functional["queue_ref"], shared_version=shared_version,
                           handoff=handoff, stack=stack, cohort=finance_cohort)
        if accounting.load(runtime.runtime_dir)[1] != book_version:
            raise ValueError("supplier_accounting_readback_changed")
        proof = {"status": "verified", "source_ref": processing.ref(op),
                "certification": {key: certification[key] for key in ("certified", "financial_ready", "allocation_complete", "status_code")} if certification else None,
                "functional": functional, "wb_authority": {"source_digest": wb["source_digest"], "source": wb["source"]},
                "economics": economics, "finance": finance,
                "publication": {"operation_id": published["operation_id"], "attempt_id": published["attempt_id"],
                    "book_version": book_version, "bundle_version": published["bundle_version"],
                    "ready_as_of_date": published["as_of_date"], "business_date": day,
                    "after_digest": published["after_digest"], "finished_at": published["finished_at"]}}
        if functional.get("origin_before_version_id") and op["action"] != "factual_date":
            from packages.application import operator_supplier_history_candidate as candidate
            from datetime import datetime, timezone
            if dated_history:
                evaluation = candidate.prepare(runtime, operation_id, now=now or datetime.now(timezone.utc),cohort_refs=dated_history['manifest']['candidate']['cohort_refs'])
            if evaluation["effect_dates"]:
                raise ValueError("supplier_historical_publication_required:"+evaluation["effect_dates"][0])
            proof["native_cost_evaluation"] = {key:evaluation[key] for key in (
                "contract", "source_ref", "functional", "queue_ref", "shipment", "receipt_dependency",
                "dates", "effect_dates", "evaluation", "current_evaluation", "source_inputs", "query_fence", "code_authority", "structural_no_effect",
                "cohort_refs", "member_evaluation")}
            current_effect = evaluation["current_evaluation"]
            own=evaluation['member_evaluation'][operation_id]
            proof["effect"] = ("derived_no_change" if own['current_no_change'] and all(value['before']==value['after'] for value in own['dated'].values())
                               else "current_cost_published")
        if dated_history:
            proof['supplier_history']={'operation_id':dated_history['manifest']['operation_id'],'attempt_id':dated_history['manifest']['attempt_id'],'manifest_digest':dated_history['manifest']['manifest_digest'],'native_history':dated_history['ack']['native'],'dates':dated_history['manifest']['candidate']['effect_dates']}
            own=dated_history['manifest']['candidate']['member_evaluation'][operation_id]
            proof['effect']='derived_no_change' if own['current_no_change'] and all(value['before']==value['after'] for value in own['dated'].values()) else 'dated_cost_published'
        conn.commit()
        if token(conn) != before:
            raise ValueError("supplier_read_snapshot_changed")
        if handoff is not None:
            handoff.append((conn, token, before))
            book_conn = stack.enter_context(closing(source.readonly(accounting.path(runtime.runtime_dir))))
            book_before = token(book_conn)
            pointer = book_conn.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()[0]
            book_conn.commit()
            if pointer != book_version or token(book_conn) != book_before:
                raise ValueError("supplier_accounting_readback_changed")
            handoff.append((book_conn, token, book_before))
        return proof
