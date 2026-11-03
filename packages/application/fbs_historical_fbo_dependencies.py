"""Query-only preparation of exact FBS dependencies for historical native work.

This deliberately does not repair identity registries or replay orders. A
historical transfer cannot silently acquire the physical/capital effects of
unrelated post-cutover orders merely because its present stock is sufficient.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application import wb_fbs_orders as orders

CONTRACT = "historical_fbo_fbs_dependency_preparation_v1"
MAX_ORDERS = 1000
MAX_STATUSES = 10000


def _digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def prepare_fbs_dependencies(conn, *, facility_id, nm_id):
    """Capture the exact guard cohort and immutable source; never mutate it.

    Caller owns one stable read transaction. Missing source tables are explicit
    blockers, so a partial rehearsal copy cannot certify zero dependencies.
    """
    if not conn.in_transaction or conn.row_factory is not sqlite3.Row:
        raise ValueError("historical_fbs_dependencies_bound_snapshot_required")
    required = {orders.OBSERVATIONS_TABLE, orders.STATUS_CURRENT_TABLE,
        orders.STATUS_OBSERVATIONS_TABLE, orders.WAREHOUSE_MAPPINGS_TABLE,
        orders.IDENTITY_MAPPINGS_TABLE, orders.IDENTITY_EVIDENCE_TABLE,
        lifecycle.CURRENT_TABLE, lifecycle.IDENTITY_PENDING_TABLE,
        lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE,
        "sheet_vitrina_v1_ff_pool_cutover_manifests"}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - tables)
    result = dict(contract=CONTRACT, facility_id=str(facility_id), nm_id=int(nm_id),
        status="blocked", blockers=[], mutates_wb=False, mutates_database=False)
    if missing:
        return {**result, "blockers": ["immutable_dependency_source_missing"], "missing_tables": missing}
    manifest_row = conn.execute("SELECT manifest_json FROM sheet_vitrina_v1_ff_pool_cutover_manifests "
        "ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1").fetchone()
    if manifest_row is None:
        return {**result, "blockers": ["cutover_manifest_missing"]}
    manifest = json.loads(manifest_row[0])
    cohort = [dict(r) for r in conn.execute(f"""SELECT observation.*,
            status.supplier_status,status.wb_status
        FROM {orders.OBSERVATIONS_TABLE} observation
        JOIN {orders.WAREHOUSE_MAPPINGS_TABLE} mapping
          ON mapping.seller_warehouse_id=observation.warehouse_id AND mapping.active=1
        LEFT JOIN {orders.STATUS_CURRENT_TABLE} status USING(order_id)
        LEFT JOIN {lifecycle.CURRENT_TABLE} current
          ON current.order_id=observation.order_id AND current.facility_id=mapping.facility_id
         AND current.pool='FBS'
        WHERE mapping.facility_id=? AND observation.nm_id=?
          AND observation.observation_sequence=(SELECT MAX(newer.observation_sequence)
              FROM {orders.OBSERVATIONS_TABLE} newer WHERE newer.order_id=observation.order_id)
          AND COALESCE(status.supplier_status,'')<>'cancel'
          AND COALESCE(status.wb_status,'') NOT IN
              ('sold','accepted_by_client','canceled','canceled_by_client','declined_by_client','defect')
          AND COALESCE(current.state,'') NOT IN
              ('reserved','released','fulfilled','fulfilled_reconciliation','cancelled_noop','late_pre_t_isolated')
        ORDER BY observation.order_id LIMIT ?""", (str(facility_id), int(nm_id), MAX_ORDERS + 1))]
    if len(cohort) > MAX_ORDERS:
        raise ValueError("historical_fbs_dependencies_order_budget")
    source = []
    blocker_set = set()
    boundary = manifest.get("accounting_boundary") or {}
    boundary_at = boundary.get("local_boundary_at", manifest["cutover_at"])
    watermark = int(boundary.get("status_observation_watermark_sequence", 0))
    for row in cohort:
        statuses = [dict(s) for s in conn.execute(f"SELECT * FROM {orders.STATUS_OBSERVATIONS_TABLE} "
            "WHERE order_id=? ORDER BY observation_sequence", (row["order_id"],))]
        if sum(len(s["statuses"]) for s in source) + len(statuses) > MAX_STATUSES:
            raise ValueError("historical_fbs_dependencies_status_budget")
        identities = [dict(s) for s in conn.execute(f"SELECT * FROM {orders.IDENTITY_EVIDENCE_TABLE} "
            "WHERE order_id=? ORDER BY evidence_sequence", (row["order_id"],))]
        pending = [dict(s) for s in conn.execute(f"SELECT pending.* FROM {lifecycle.IDENTITY_PENDING_TABLE} pending "
            f"LEFT JOIN {lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE} resolved USING(pending_id) "
            "WHERE pending.order_id=? AND pending.cutover_id=? AND resolved.pending_id IS NULL "
            "ORDER BY pending.source_status_observation_sequence", (row["order_id"], manifest["cutover_id"]))]
        if not statuses:
            blocker_set.add("immutable_order_status_missing")
        if not any(s["outcome"] == "matched" for s in identities):
            blocker_set.add("exact_matched_identity_missing")
        if row["observed_at"] > boundary_at:
            blocker_set.add("post_cutover_lifecycle_outside_native_transfer_scope")
        # Preserve all statuses, including a past complete/sorted observation
        # whose current ready_for_pickup status alone cannot prove the handoff.
        source.append(dict(order=row, statuses=statuses, identity_evidence=identities,
            unresolved_identity_pending=pending,
            first_observed_at=min((s["observed_at"] for s in statuses), default=None),
            status_before_frozen_watermark=any(s["observation_sequence"] <= watermark for s in statuses),
            physical_handoff_observed=any(s["supplier_status"] == "complete" and s["wb_status"] == "sorted" for s in statuses)))
    if cohort:
        blocker_set.add("native_owned_lifecycle_reconciliation_required")
    material = dict(cutover_id=manifest["cutover_id"], boundary_at=boundary_at,
        frozen_status_watermark=watermark, orders=source)
    return {**result, "status": "blocked" if blocker_set else "ready",
        "blockers": sorted(blocker_set), "order_count": len(cohort),
        "status_count": sum(len(s["statuses"]) for s in source),
        "orders_with_physical_handoff": sum(s["physical_handoff_observed"] for s in source),
        "source": material, "source_digest": _digest(material)}
