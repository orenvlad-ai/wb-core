"""Private-copy, connection-owned exact FBS dependency recovery.

The immutable recipe explicitly adds the admitted order handoffs to the native
transfer scope. The existing collector owns matched evidence and the existing
pinned lifecycle owns every reservation, handoff and material publication.
There is no global drain, fabricated pre-T classification, or WB call.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from packages.application import fbs_historical_fbo_dependencies as dependencies
from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application import wb_fbs_orders as orders
from packages.application.ff_fbs_mapping_extension_production import _identity_plan
from packages.application.ff_pool_foundation import BALANCES_TABLE, LINES_TABLE, OPERATIONS_TABLE, canonical_decimal_text

CONTRACT = "private_historical_fbo_scoped_fbs_recovery_v1"
RECEIPTS = "private_historical_fbo_scoped_fbs_recovery_receipts"
PRODUCTION_RECEIPTS = "sheet_vitrina_v1_ff_pool_fbs_scoped_recovery_receipts"
CURSORS = (lifecycle.DRAIN_STATE_TABLE, lifecycle.FORWARD_GENERATIONS_TABLE, lifecycle.FORWARD_STATE_TABLE)


def require(value, code):
    if not value:
        raise ValueError("historical_fbs_scoped_recovery_" + code)


def _rows(conn, table, where="1", args=()):
    return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid', args)]


def _cursor_state(conn):
    return {table: _rows(conn, table) for table in CURSORS}


def _unrelated(conn, facility, nm):
    return {table: dependencies._digest(_rows(conn, table, "nm_id<>? OR facility_id<>?", (nm, facility)))
        for table in (BALANCES_TABLE, lifecycle.CURRENT_TABLE, lifecycle.EVENTS_TABLE)}


def _bound(conn):
    require(conn.in_transaction and conn.row_factory is sqlite3.Row, "bound_snapshot_required")


def prepare_scoped_fbs_recovery(conn, *, operation_id, facility_id, nm_id):
    """Plan the exact unresolved cohort, canonical identity and monetary effects."""
    _bound(conn)
    require(isinstance(operation_id, str) and 0 < len(operation_id) <= 160, "operation_id_invalid")
    diagnostic = dependencies.prepare_fbs_dependencies(conn, facility_id=facility_id, nm_id=nm_id)
    require("source" in diagnostic and diagnostic["order_count"] > 0, "exact_unresolved_cohort_required")
    manifest = json.loads(conn.execute("SELECT manifest_json FROM sheet_vitrina_v1_ff_pool_cutover_manifests "
        "WHERE cutover_id=?", (diagnostic["source"]["cutover_id"],)).fetchone()[0])
    lifecycle._require_approved_policy(manifest)
    source_orders, statuses = [], []
    for item in diagnostic["source"]["orders"]:
        revisions = _rows(conn, orders.OBSERVATIONS_TABLE, "order_id=?", (item["order"]["order_id"],))
        require(revisions and all(str(r["observed_at"]) > diagnostic["source"]["boundary_at"]
            and str(r["source_created_at"]) > diagnostic["source"]["boundary_at"] for r in revisions), "pre_t_or_missing_identity_boundary")
        require(not item["status_before_frozen_watermark"], "pre_frozen_status_outside_scope")
        require(all(r["nm_id"] == int(nm_id) for r in revisions), "order_sku_revision_changed")
        source_orders.extend(revisions)
        statuses.extend(item["statuses"])
    require(len({r["observation_sequence"] for r in statuses}) == len(statuses), "duplicate_status_identity")
    statuses.sort(key=lambda r: r["observation_sequence"])
    for status in statuses:
        require(any(r["order_id"] == status["order_id"] and r["source_revision"] == status["order_revision"]
                    for r in source_orders), "exact_status_revision_missing")
    identity_plan, blockers = _identity_plan(conn, source_orders)
    require(not blockers and identity_plan and all(r["target_nm_id"] == int(nm_id) for r in identity_plan), "canonical_identity_unproved:" + json.dumps(blockers))
    warehouse_proof = []
    for warehouse_id in sorted({r["warehouse_id"] for r in source_orders}):
        mappings = _rows(conn, orders.WAREHOUSE_MAPPINGS_TABLE, "seller_warehouse_id=? AND active=1", (warehouse_id,))
        require(mappings and {r["facility_id"] for r in mappings} == {facility_id}, "warehouse_revision_outside_scope")
        warehouse_proof.extend(mappings)
    source_balance = conn.execute(f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=? AND projection_epoch=?",
        (facility_id, int(nm_id), manifest["feature_epoch"])).fetchone()
    require(source_balance is not None, "source_balance_missing")
    # Predict with the exact lifecycle owner's current-capital ratio, once at
    # each serialized handoff boundary. Never price from a historic book WAC.
    quantity, capital = int(source_balance["quantity"]), Decimal(source_balance["capital_rub"])
    fulfilled, predicted = set(), []
    for status in statuses:
        if status["order_id"] in fulfilled or not lifecycle._is_handoff(status["supplier_status"], status["wb_status"]):
            continue
        amount = int(status["positive_quantity"])
        require(quantity >= amount and amount > 0, "handoff_stock_insufficient")
        wac = lifecycle._stored_wac_ratio(capital, quantity)
        delta = lifecycle._capital_delta(wac, -amount)
        quantity -= amount; capital = lifecycle._decimal_sum(capital, delta)
        require(quantity >= 0 and capital >= 0 and ((quantity == 0) == (capital == 0)), "handoff_balance_shape_invalid")
        predicted.append(dict(order_id=status["order_id"], status_observation_sequence=status["observation_sequence"],
            quantity_delta=-amount, capital_delta_rub=canonical_decimal_text(delta), wac_rub=canonical_decimal_text(wac)))
        fulfilled.add(status["order_id"])
    active = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1").fetchone())
    material = dict(contract=CONTRACT, operation_id=operation_id, facility_id=str(facility_id), nm_id=int(nm_id),
        diagnostic=diagnostic, source_orders=source_orders, statuses=statuses, identity_plan=identity_plan,
        manifest=manifest, source_balance=dict(source_balance), source_active=active, warehouse_proof=warehouse_proof,
        source_active_rows=_rows(conn, "sheet_vitrina_v1_warehouse_functional_balances", "version_id=?", (active["version_id"],)),
        cursors=_cursor_state(conn), unrelated=_unrelated(conn, facility_id, int(nm_id)),
        predicted_handoffs=predicted, predicted_after=dict(quantity=quantity, capital_rub=canonical_decimal_text(capital)))
    require(len(json.dumps(material).encode()) <= 16 * 1024**2, "recipe_size_budget")
    return {**material, "recipe_digest": dependencies._digest(material)}


def apply_scoped_fbs_recovery(conn, *, recipe, runtime_dir, occurred_at, actor):
    """Caller holds writer locks and transaction; only an isolated copy is legal.

    The caller must rollback on every error, including post-write verification.
    This helper neither starts nor commits a transaction or background work.
    """
    _bound(conn)
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    root = Path(runtime_dir).resolve()
    require("private-evidence" in path.parts and path.is_relative_to(root), "private_copy_required")
    return apply_scoped_fbs_recovery_owned(conn, recipe=recipe, occurred_at=occurred_at,
        actor=actor, receipt_table=RECEIPTS)


def ensure_scoped_recovery_receipts(conn):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {PRODUCTION_RECEIPTS}(operation_id TEXT PRIMARY KEY,recipe_digest TEXT NOT NULL,result_json TEXT NOT NULL)")


def apply_scoped_fbs_recovery_owned(conn, *, recipe, occurred_at, actor, receipt_table):
    """Shared connection core; a production caller must own reviewed admission.

    This function owns no target selection, lock, transaction, or commit. The
    registered production adapter supplies those; the private wrapper above
    retains its separate immutable-copy restriction.
    """
    _bound(conn)
    require(receipt_table in (RECEIPTS, PRODUCTION_RECEIPTS), "receipt_owner_invalid")
    if receipt_table == PRODUCTION_RECEIPTS:
        from packages.application.fbs_scoped_lifecycle_production import require_production_core_admission
        require_production_core_admission(conn,recipe)
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    lifecycle._require_utc(occurred_at)
    require(isinstance(actor, str) and actor.strip(), "actor_required")
    require(recipe["contract"] == CONTRACT and recipe["recipe_digest"] == dependencies._digest(
        {k:v for k,v in recipe.items() if k != "recipe_digest"}), "recipe_digest_invalid")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {receipt_table}(operation_id TEXT PRIMARY KEY,recipe_digest TEXT NOT NULL,result_json TEXT NOT NULL)")
    old = conn.execute(f"SELECT * FROM {receipt_table} WHERE operation_id=?", (recipe["operation_id"],)).fetchone()
    if old:
        return verify_scoped_recovery_receipt(conn, recipe=recipe, receipt_table=receipt_table)
    fresh = prepare_scoped_fbs_recovery(conn, operation_id=recipe["operation_id"], facility_id=recipe["facility_id"], nm_id=recipe["nm_id"])
    require(fresh == recipe, "source_compare_and_swap_failed")
    before_event = int(conn.execute(f"SELECT COALESCE(MAX(event_sequence),0) FROM {lifecycle.EVENTS_TABLE}").fetchone()[0])
    for item in recipe["identity_plan"]:
        if item["action"] == "noop":
            continue
        material = {k:item[k] for k in ("source_nm_id", "source_chrt_id", "source_barcode", "source_sku", "target_nm_id")}
        digest = dependencies._digest(material)
        conn.execute(f"INSERT INTO {orders.IDENTITY_MAPPINGS_TABLE}(mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,target_nm_id,mapping_digest,active,created_at,created_by) VALUES(?,?,?,?,?,?,?,1,?,?)",
            ("fbs_sku_" + digest[7:39], item["source_nm_id"], item["source_chrt_id"], item["source_barcode"], item["source_sku"], item["target_nm_id"], digest, occurred_at, actor))
    # Use the collector's existing immutable evidence writer on the exact
    # original revisions only; construction performs no source/API operation.
    collector = orders.WbFbsOrdersCollector(db_path=path, timestamp_factory=lambda: occurred_at, source=object(), enabled=False)
    collector._persist_identity_evidence(conn, [{**row, "observed_at": occurred_at} for row in recipe["source_orders"]])
    sequences = tuple(r["observation_sequence"] for r in recipe["statuses"])
    from packages.application.warehouse_business_projection import ready_binding_cache
    with ready_binding_cache(conn):
        recovered = lifecycle.recover_pinned_fbs_lifecycle(conn, manifest=recipe["manifest"],
            status_observation_sequences=sequences, occurred_at=occurred_at)
    require(recovered["status"] == "recovered" and recovered["identity_pending_count"] == 0, "native_lifecycle_incomplete")
    require(_cursor_state(conn) == recipe["cursors"], "global_cursor_changed")
    require(_unrelated(conn, recipe["facility_id"], recipe["nm_id"]) == recipe["unrelated"], "unrelated_native_scope_changed")
    events = _rows(conn, lifecycle.EVENTS_TABLE, "event_sequence>?", (before_event,))
    require(all(e["order_id"] in {r["order_id"] for r in recipe["source_orders"]}
        and e["facility_id"] == recipe["facility_id"] and e["nm_id"] == recipe["nm_id"] for e in events), "event_scope_expanded")
    actual = [dict(order_id=e["order_id"], status_observation_sequence=e["source_status_observation_sequence"],
        quantity_delta=e["physical_quantity_delta"], capital_delta_rub=e["capital_delta_rub"], wac_rub=e["frozen_wac_rub"])
        for e in events if e["physical_quantity_delta"]]
    require(actual == recipe["predicted_handoffs"], "declared_handoff_money_changed")
    balance = conn.execute(f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?",
        (recipe["facility_id"], recipe["nm_id"])).fetchone()
    require(dict(balance) == recipe["predicted_after"], "final_balance_mismatch")
    from packages.application.operator_warehouse_documents import assert_reservations
    assert_reservations(conn, plan={"documents":[{"movements":[dict(facility_id=recipe["facility_id"], pool="FBS", nm_id=recipe["nm_id"], quantity_delta=-1)]}]}, epoch=recipe["manifest"]["feature_epoch"])
    native_receipts = {OPERATIONS_TABLE: [], LINES_TABLE: []}
    for event in events:
        if not event["physical_quantity_delta"]:
            continue
        native = _rows(conn, OPERATIONS_TABLE, "source_revision=? AND source_type='fbs_order_lifecycle_event'", (event["event_id"],))
        require(len(native) == 1, "native_handoff_receipt_missing")
        native_receipts[OPERATIONS_TABLE].extend(native)
        lines = _rows(conn, LINES_TABLE, "operation_id=?", (native[0]["operation_id"],))
        require(len(lines) == 1 and lines[0]["quantity_delta"] == event["physical_quantity_delta"]
            and lines[0]["capital_delta_rub"] == event["capital_delta_rub"], "native_handoff_line_mismatch")
        native_receipts[LINES_TABLE].extend(lines)
    ids = sorted({row["order_id"] for row in recipe["source_orders"]})
    slots = ','.join('?' for _ in ids)
    identity_receipts = {
        orders.IDENTITY_MAPPINGS_TABLE: _rows(conn, orders.IDENTITY_MAPPINGS_TABLE, "source_nm_id=?", (recipe["nm_id"],)),
        orders.IDENTITY_EVIDENCE_TABLE: _rows(conn, orders.IDENTITY_EVIDENCE_TABLE, f"order_id IN ({slots}) AND outcome='matched'", ids),
        lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE: _rows(conn, lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE, f"order_id IN ({slots}) AND cutover_id=?", [*ids,recipe["manifest"]["cutover_id"]]),
    }
    require(identity_receipts[orders.IDENTITY_MAPPINGS_TABLE] and identity_receipts[orders.IDENTITY_EVIDENCE_TABLE], "identity_receipt_missing")
    result = dict(contract=CONTRACT, operation_id=recipe["operation_id"], recipe_digest=recipe["recipe_digest"],
        status="private_dependencies_recovered" if receipt_table == RECEIPTS else "dependencies_recovered", idempotent=False, order_count=recipe["diagnostic"]["order_count"],
        status_count=len(sequences), lifecycle=recovered, events=events, physical_handoffs=actual,
        source_balance=recipe["source_balance"], final_balance=dict(balance), native_receipts=native_receipts, identity_receipts=identity_receipts, global_cursors_unchanged=True,
        unrelated_native_scope_unchanged=True, mutates_wb=False, publication_complete=False)
    conn.execute(f"INSERT INTO {receipt_table} VALUES(?,?,?)", (recipe["operation_id"], recipe["recipe_digest"], json.dumps(result, sort_keys=True)))
    return result


def verify_scoped_recovery_receipt(conn, *, recipe, receipt_table, check_current_dependencies=True):
    """Strictly query-only durable replay proof, usable after commit/reopen."""
    _bound(conn)
    require(receipt_table in (RECEIPTS, PRODUCTION_RECEIPTS), "receipt_owner_invalid")
    old = conn.execute(f"SELECT * FROM {receipt_table} WHERE operation_id=?", (recipe["operation_id"],)).fetchone()
    require(old is not None, "durable_receipt_missing")
    require(old["recipe_digest"] == recipe["recipe_digest"], "existing_recipe_conflict")
    result = json.loads(old["result_json"])
    for event in result["events"]:
        stored = conn.execute(f"SELECT * FROM {lifecycle.EVENTS_TABLE} WHERE event_id=?", (event["event_id"],)).fetchone()
        require(stored is not None and dict(stored) == event, "existing_event_conflict")
    for table in (OPERATIONS_TABLE, LINES_TABLE):
        for row in result["native_receipts"][table]:
            key = "operation_id=?" + (" AND line_no=?" if table == LINES_TABLE else "")
            args = (row["operation_id"], row["line_no"]) if table == LINES_TABLE else (row["operation_id"],)
            require(_rows(conn, table, key, args) == [row], "existing_native_receipt_conflict")
    for table, primary in ((orders.IDENTITY_MAPPINGS_TABLE, "mapping_id"),
            (orders.IDENTITY_EVIDENCE_TABLE, "evidence_sequence"),
            (lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE, "resolution_id")):
        for row in result["identity_receipts"][table]:
            require(_rows(conn, table, primary + "=?", (row[primary],)) == [row], "existing_identity_receipt_conflict")
    if check_current_dependencies:
        remaining = dependencies.prepare_fbs_dependencies(conn, facility_id=recipe["facility_id"], nm_id=recipe["nm_id"])
        require(remaining.get("status") == "ready" and remaining.get("order_count") == 0, "existing_dependency_guard_changed")
    return {**result, "idempotent": True}
