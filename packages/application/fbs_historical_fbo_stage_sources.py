"""Pinned, query-only operands for the bounded historical FBO stage adapter.

This captures saved native rows, including foreign acceptance contributions and
actual WB snapshots. Missing dates remain absent. It does not rebuild money,
collect sources, bootstrap a schema, construct a runtime or publish a version.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
import json
import sqlite3

from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_historical_fbo_gap import build_historical_fbo_state_revision
from packages.application.fbs_historical_fbo_native import operation_id
from packages.application.fbs_historical_fbo_sources import capture_historical_fbo_documents
from packages.application.fbs_inventory_presentation import RETAINED_STAGES, retained_stage_operands
from packages.application.shared_sku_cost_sources import capture_wb_component, _digest, _json
from packages.application.sheet_vitrina_v1_own_product_capital import own_stage_metric_key
from packages.application.warehouse_business_projection import _metric_rows
from packages.application.warehouse_functional import (
    FUNCTIONAL_CUTOVER_ID, STAGES, _initial_valuation_anchor, _stable_id, _wb_snapshot_integrity,
)
from packages.application.warehouse_archival_estimate import overlay_opening_cost_rows
from packages.application.wb_initial_fbs_cost import valid_anchor
from packages.business_time import business_date_from_timestamp

CONTRACT = "historical_fbo_stage_sources_v1"
P = "sheet_vitrina_v1_warehouse_"
MAX_ROWS = 100000
MAX_BYTES = 64 * 1024**2
MAX_DAYS = 366
MAX_SKUS = 500
STAGE_KEYS = {"PRODUCTION": "production", "PRODUCTION_TO_FF": "china_to_ff",
              "FF": "ff", "FF_TO_WB": "ff_to_wb", "WB": "wb",
              "WB_ACCEPTANCE_DISCREPANCY": "wb_acceptance_discrepancy"}


def require(value, code):
    if not value:
        raise ValueError("historical_fbo_stage_sources_" + code)


def _decimal(value):
    require(value is not None and value != "" and not isinstance(value, bool), "money_missing")
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, "money_invalid")
    return result


class _Reader:
    def __init__(self, conn):
        self.conn, self.queries, self.bytes = conn, [], 0

    def rows(self, sql, args=()):
        """Stream bounded rows; retain the exact fixed query for later CAS."""
        result = []
        for row in self.conn.execute(sql, tuple(args)):
            require(len(result) < MAX_ROWS, "row_limit")
            item = dict(row)
            self.bytes += len(canonical(item).encode("utf-8"))
            require(self.bytes <= MAX_BYTES, "source_byte_limit")
            result.append(item)
        self.queries.append((sql, tuple(args)))
        return result

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        require(len(rows) == 1, "single_native_row_required")
        return rows[0]


def _parsed_balances(rows):
    return [{**{k: v for k, v in row.items() if k != "provenance_json"},
             "provenance": json.loads(row["provenance_json"])} for row in rows]


def _snapshot_quantity_authority(version, snapshot):
    """Authenticate primary quantity without requiring a factual cost layer."""
    day = snapshot["snapshot_date"]
    raw = json.loads(snapshot["raw_rows_json"])
    items = json.loads(snapshot["items_json"])
    requested = json.loads(snapshot["requested_nm_ids_json"])
    require(version["status"] == "good" and version["business_effective_date"] == day
            and business_date_from_timestamp(snapshot["fetched_at"]) == day
            and snapshot["pagination_complete"] == 1 and type(requested) is list
            and requested and all(type(nm) is int and nm > 0 for nm in requested)
            and len(set(requested)) == len(requested)
            and {r["nm_id"] for r in items} == set(requested) and len(items) == len(requested)
            and snapshot["raw_row_count"] == len(raw)
            and snapshot["raw_rows_digest"] == _digest(sorted(raw, key=_json)),
            "snapshot_quantity_authority_invalid")
    for item in items:
        values = [_decimal(item[k]) for k in ("quantity", "in_way_to_client", "in_way_from_client")]
        require(all(v == v.to_integral_value() for v in values)
                and _decimal(item["wb_contour_quantity"]) == sum(values, Decimal(0)), "snapshot_quantity_components_invalid")
    integrity = _wb_snapshot_integrity(snapshot)
    watermark = json.loads(version["source_watermarks_json"]).get("wb_snapshot", {})
    require(integrity["raw_to_canonical_mapping_matches"] and not integrity["exact_duplicate_count"]
            and not integrity["source_key_duplicate_count"] and watermark.get("snapshot_id")
            and snapshot["version_id"] == version["version_id"]
            and snapshot["snapshot_id"] == _stable_id("wbsnapv", {"source_snapshot_id": watermark["snapshot_id"], "version_id": version["version_id"]})
            and watermark.get("digest") == snapshot["raw_rows_digest"]
            and watermark.get("fetched_at") == snapshot["fetched_at"]
            and watermark.get("pagination_complete") is True
            and watermark.get("raw_row_count") == len(raw) and watermark.get("requested_count") == len(requested),
            "snapshot_raw_mapping_or_watermark_invalid")


def _date_stage(reader, book, day):
    wb, retained = book["wb_days"][day], book["retained_days"][day]
    version_id = wb["version_id"]
    version = reader.one(f"SELECT * FROM {P}functional_versions WHERE version_id=?", (version_id,))
    require(version["status"] == "good" and version["business_effective_date"] == day,
            "saved_version_or_date_changed")
    nms = sorted(int(nm) for nm in retained["rows"])
    exact = capture_wb_component(None, day=day, nm_ids=nms, version_id=version_id, connection=reader.conn)
    require(exact == wb and exact["complete"] and exact["authority_complete"], "saved_wb_authority_changed")
    snapshot = reader.one(f"SELECT * FROM {P}wb_snapshots WHERE version_id=?", (version_id,))
    balances = _parsed_balances(reader.rows(
        f"SELECT * FROM {P}functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id", (version_id,)))
    require(len({(r["warehouse_key"], r["nm_id"]) for r in balances}) == len(balances), "duplicate_balance")
    models = reader.rows(f"SELECT * FROM {P}functional_read_models WHERE version_id=? ORDER BY warehouse_key", (version_id,))
    require(len(models) == 6 and {r["warehouse_key"] for r in models} == set(STAGES), "six_stage_models_required")
    with localcontext() as context:
        context.prec = 160
        for model in models:
            payload = json.loads(model["payload_json"])
            expected = {int(r["nm_id"]): (_decimal(r["quantity"]), _decimal(r["capital_rub"]))
                        for r in balances if r["warehouse_key"] == model["warehouse_key"]}
            observed = {int(r["nm_id"]): (_decimal(r["quantity"]), _decimal(r["capital_rub"]))
                        for r in payload.get("balances", [])}
            require(payload.get("status") == "ready" and observed == expected
                    and len(observed) == len(payload.get("balances", []))
                    and _decimal(payload["warehouse"]["total_quantity"]) == sum((v[0] for v in expected.values()), Decimal(0))
                    and _decimal(payload["warehouse"]["total_capital_rub"]) == sum((v[1] for v in expected.values()), Decimal(0)),
                    "stage_payload_conservation_failed")
        require(retained["date"] == day and retained["wb_version_id"] == version_id
                and retained["source_digest"] == fingerprint({k: v for k, v in retained.items() if k != "source_digest"}),
                "retained_binding_changed")
        metrics = _metric_rows(balances, affected_nm_ids=nms)
        wb_rows = {int(row["nm_id"]): row for row in wb["rows"]}
        for nm in nms:
            operands = retained_stage_operands(retained["rows"][str(nm)], wb_capture=wb, expected_wb=wb_rows.get(nm, {}))
            for stage in RETAINED_STAGES:
                for key, field in (("quantity", "qty"), ("capital_rub", "capital_rub")):
                    raw = metrics[nm]["metrics"][own_stage_metric_key(stage, field)]
                    if raw is None and not any(int(r["nm_id"]) == nm for r in balances):
                        explicit = wb_rows[nm]
                        require(explicit["status"] == "available" and _decimal(explicit["quantity"]) == 0
                                and _decimal(explicit["capital_rub"]) == 0, "catalog_zero_unproved")
                        raw = 0
                    require(raw is not None and _decimal(operands[stage][key]) == Decimal(str(raw)),
                            "retained_native_operand_mismatch")
    companions = {}
    for table, order in (("functional_ff_reservations", "supply_id,nm_id"),
                         ("supplier_cost_states", "shipment_id"), ("unmatched_doprinato", "unmatched_id")):
        companions[table] = reader.rows(f"SELECT * FROM {P}{table} WHERE version_id=? ORDER BY {order}", (version_id,))
    return dict(version=version, snapshot=snapshot, balances=balances, read_models=models, companions=companions)


def _replay(reader, *, book, dates, nms, first_dispatch):
    marks = ",".join("?" for _ in nms)
    cutover = reader.one(f"SELECT * FROM {P}functional_cutovers WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
    require(cutover["status"] == "posted", "posted_cutover_required")
    cutover_version = reader.one(f"SELECT * FROM {P}functional_versions WHERE cutover_id=? "
        "AND version_kind='functional_cutover' AND status='good' ORDER BY created_at,rowid LIMIT 1", (FUNCTIONAL_CUTOVER_ID,))
    cutover_snapshot = reader.one(f"SELECT * FROM {P}wb_snapshots WHERE version_id=?", (cutover_version["version_id"],))
    _snapshot_quantity_authority(cutover_version, cutover_snapshot)
    opening_balances = reader.rows(f"SELECT * FROM {P}functional_balances WHERE version_id=? "
        f"AND warehouse_key='wb' AND nm_id IN ({marks}) ORDER BY nm_id", (cutover_version["version_id"], *nms))
    opening = reader.rows(f"SELECT * FROM {P}opening_cost_map WHERE cutover_id=? AND nm_id IN ({marks}) ORDER BY nm_id",
                          (FUNCTIONAL_CUTOVER_ID, *nms))
    require(opening, "nonempty_opening_cost_map_required")
    # The normal reader overlays the existing approved archival estimate. Its
    # complete immutable active source is small but still bounded and fenced.
    for table, order in (("archival_estimate_active", "slot"), ("archival_estimate_versions", "version_id"),
                         ("archival_estimate_rows", "version_id,nm_id")):
        reader.rows(f"SELECT * FROM {P}{table} ORDER BY {order}")
    opening_map = overlay_opening_cost_rows(reader.conn,
        ({**row, "provenance": json.loads(row["provenance_json"])} for row in opening))
    start = business_date_from_timestamp(cutover["cutover_at"])
    last = dates[-1]
    require(0 <= (date.fromisoformat(last) - date.fromisoformat(start)).days <= MAX_DAYS,
            "replay_calendar_limit")
    latest = reader.rows(f"WITH ranked AS (SELECT s.snapshot_date,v.version_id,"
        "ROW_NUMBER() OVER(PARTITION BY s.snapshot_date ORDER BY v.effective_at DESC,v.created_at DESC,v.rowid DESC) rank "
        f"FROM {P}functional_versions v JOIN {P}wb_snapshots s USING(version_id) "
        "WHERE v.cutover_id=? AND v.status='good' AND s.snapshot_date>=? AND s.snapshot_date<=?) "
        "SELECT snapshot_date,version_id FROM ranked WHERE rank=1 ORDER BY snapshot_date", (FUNCTIONAL_CUTOVER_ID, start, last))
    require(len(latest) <= MAX_DAYS, "replay_day_limit")
    chosen = {row["snapshot_date"]: row["version_id"] for row in latest}
    # The admitted book owns the real dated editions. A newer hourly edition
    # of a closed day must not silently replace that book's source pointer.
    chosen.update({day: book["wb_days"][day]["version_id"] for day in dates})
    snapshots = {}
    for day, vid in sorted(chosen.items()):
        version = reader.one(f"SELECT * FROM {P}functional_versions WHERE version_id=?", (vid,))
        snapshot = reader.one(f"SELECT * FROM {P}wb_snapshots WHERE version_id=?", (vid,))
        require(version["status"] == "good" and snapshot["snapshot_date"] == day
                and version["business_effective_date"] == day and snapshot["pagination_complete"] == 1,
                "actual_snapshot_binding_changed")
        _snapshot_quantity_authority(version, snapshot)
        snapshots[day] = dict(version=version, snapshot=snapshot)
    anchors = reader.rows(f"WITH ranked AS (SELECT b.nm_id,b.version_id,"
        "ROW_NUMBER() OVER(PARTITION BY b.nm_id ORDER BY v.effective_at,v.created_at,v.rowid) rank "
        f"FROM {P}functional_versions v JOIN {P}functional_balances b USING(version_id) "
        "WHERE v.cutover_id=? AND v.status='good' AND b.warehouse_key='wb' "
        f"AND b.nm_id IN ({marks}) AND b.provenance_json LIKE '%initial_valuation_anchor%' "
        "AND v.business_effective_date<=?) SELECT nm_id,version_id FROM ranked WHERE rank=1 ORDER BY nm_id",
        (FUNCTIONAL_CUTOVER_ID, *nms, last))
    initial = []
    for item in anchors:
        vid, nm = item["version_id"], item["nm_id"]
        version = reader.one(f"SELECT * FROM {P}functional_versions WHERE version_id=?", (vid,))
        snapshot = reader.one(f"SELECT * FROM {P}wb_snapshots WHERE version_id=?", (vid,))
        _snapshot_quantity_authority(version, snapshot)
        balance = reader.one(f"SELECT * FROM {P}functional_balances WHERE version_id=? AND warehouse_key='wb' AND nm_id=?", (vid, nm))
        anchor = _initial_valuation_anchor(json.loads(balance["provenance_json"]))
        with localcontext() as context:
            # The native initial WB balance materializes q * anchor at 28;
            # source WAC can carry more documentary digits than that row.
            context.prec = 28
            require(anchor and int(anchor.get("nm_id") or 0) == nm and valid_anchor(anchor)
                and _stable_id("wbsnapv", {"source_snapshot_id": anchor["snapshot_id"], "version_id": vid}) == snapshot["snapshot_id"]
                and anchor["business_date"] == snapshot["snapshot_date"]
                and _decimal(balance["quantity"]) > 0
                and _decimal(balance["cost_covered_quantity"]) >= _decimal(balance["quantity"])
                and _decimal(balance["capital_rub"]) == _decimal(balance["quantity"]) * _decimal(anchor["wac_rub"]),
                "initial_valuation_anchor_invalid")
        initial.append(dict(version=version, snapshot=snapshot, balance=balance))
    events = reader.rows(f"SELECT * FROM {P}functional_events WHERE event_type='wb_final_acceptance' "
        f"AND nm_id IN ({marks}) AND business_date>=? AND business_date<=? ORDER BY business_date,created_at,event_id", [*nms, start, last])
    require(all(start <= str(row["business_date"] or "") <= last for row in events), "acceptance_event_replay_date_invalid")
    daily = reader.rows(f"SELECT * FROM {P}wb_daily_cost WHERE cutover_id=? AND as_of_date>=? AND as_of_date<=? "
        f"AND nm_id IN ({marks}) ORDER BY as_of_date,nm_id", (FUNCTIONAL_CUTOVER_ID, first_dispatch, last, *nms))
    return dict(cutover=cutover, cutover_version=cutover_version, cutover_snapshot=cutover_snapshot,
                opening_balances=opening_balances, opening_cost_rows=opening, opening_cost_map=opening_map,
                snapshots_by_day=snapshots, initial_anchors=initial, acceptance_events=events, daily_cost_rows=daily)


def _fee_sources(reader, supply_ids, fee_scope):
    """Preserve existing supply fees; accepted new uploads are not cost layers."""
    from packages.application.fulfillment_services import UPLOADS_TABLE, LINES_TABLE
    marks = ",".join("?" for _ in supply_ids)
    cache_keys = ["supply:" + sid for sid in supply_ids]
    rows = reader.rows(f"SELECT DISTINCT u.upload_id FROM {UPLOADS_TABLE} u JOIN {LINES_TABLE} l USING(upload_id) "
        f"WHERE u.validation_status='ok' AND u.deleted_at IS NULL AND l.match_status='ok' "
        f"AND (l.matched_wb_supply_id IN ({marks}) OR l.matched_wb_cache_key IN ({marks})) ORDER BY u.upload_id",
        [*supply_ids, *cache_keys])
    result = {}
    for row in rows:
        uid = row["upload_id"]
        result[uid] = dict(upload=reader.one(f"SELECT * FROM {UPLOADS_TABLE} WHERE upload_id=?", (uid,)),
            lines=reader.rows(f"SELECT * FROM {LINES_TABLE} WHERE upload_id=? ORDER BY row_index,id", (uid,)))
    expected = fee_scope["uploads"]
    require(len(expected) == 3 and len({r["upload_id"] for r in expected}) == 3
            and {r["wb_supply_id"] for r in expected} == set(supply_ids), "three_accepted_fee_sources_required")
    require(all(r["upload_id"] in result and result[r["upload_id"]]["upload"]["file_sha256"] == r["file_sha256"]
                for r in expected), "accepted_fee_source_changed")
    return result


def capture_historical_fbo_stages(conn, *, book, native_plan, accounting_capture, fee_scope):
    require(conn.in_transaction and conn.row_factory is sqlite3.Row
            and conn.execute("PRAGMA query_only").fetchone()[0] == 1, "pinned_query_only_required")
    reader = _Reader(conn)
    native_size = reader.one("SELECT count(*) n,coalesce(sum(length(CAST(metadata_json AS BLOB))),0) bytes "
        "FROM sheet_vitrina_v1_warehouse_business_operations WHERE source_system='historical_fbo_dispatch'")
    require(native_size["n"] == 4 and native_size["bytes"] <= MAX_BYTES, "native_source_byte_limit_or_cohort")
    state_revision = build_historical_fbo_state_revision(book, accounting_capture)
    require(native_plan["source_proof"]["book_version"] == fingerprint(book), "native_book_binding_changed")
    typed = capture_historical_fbo_documents(conn)
    require(len(typed) == 4 and {d["historical_fbo_native"]["plan_digest"] for d in typed} == {native_plan["plan_digest"]},
            "exact_native_cohort_required")
    require([d for d in accounting_capture["documents"] if d.get("historical_fbo_native")] == typed,
            "accounting_native_documents_changed")
    dates = state_revision["derived_dates_to_rebuild"]
    require(0 < len(dates) <= MAX_DAYS and dates[-1] == accounting_capture["business_date"], "real_date_scope_invalid")
    supply_ids = sorted(e["source_key"].rsplit(":", 1)[1] for e in native_plan["effects"] if e["kind"] == "historical_wb_dispatch")
    nms = sorted({line["nm_id"] for e in native_plan["effects"] for line in e["pool_movements"]})
    require(len(supply_ids) == len(set(supply_ids)) == 3 and 0 < len(nms) <= MAX_SKUS, "supply_scope_invalid")
    ids = tuple(operation_id(native_plan, i) for i in range(4)); marks = ",".join("?" for _ in ids)
    for table, order in (("warehouse_business_operations", "operation_id"), ("ff_pool_movement_lines", "operation_id,line_no"),
                         ("ff_stock_operations", "operation_id"), ("ff_stock_operation_lines", "operation_id,line_no")):
        reader.rows(f"SELECT * FROM sheet_vitrina_v1_{table} WHERE operation_id IN ({marks}) ORDER BY {order}", ids)
    reader.rows("SELECT operation_id FROM sheet_vitrina_v1_warehouse_business_operations "
                "WHERE source_system='historical_fbo_dispatch' ORDER BY operation_id")
    dated = {day: _date_stage(reader, book, day) for day in dates}
    supplies = {}
    for sid in supply_ids:
        rows = reader.rows("SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? OR cache_key=? OR wb_supply_id=? ORDER BY supply_id",
                           (sid, "supply:" + sid, sid))
        require(len(rows) == 1 and rows[0]["supply_id"] == sid and rows[0]["cache_key"] == "supply:" + sid
                and rows[0]["wb_supply_id"] == sid, "supply_alias_or_identity_changed")
        supplies[sid] = dict(rawsource=rows[0])
    smarks = ",".join("?" for _ in supply_ids)
    costs = reader.rows("SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers "
        f"WHERE is_current=1 AND wb_supply_id IN ({smarks}) ORDER BY wb_supply_id,nm_id", supply_ids)
    require(len({(r["wb_supply_id"], r["nm_id"]) for r in costs}) == len(costs), "duplicate_current_cost_layer")
    require(fee_scope["contract"] == "historical_fbo_exact_fulfillment_fee_scope_v1"
            and len(fee_scope["requests"]) == 3
            and {r["wb_supply_id"] for r in fee_scope["requests"]} == set(supply_ids)
            and 0 < len(fee_scope["query_fence"]) <= 100, "fee_scope_changed")
    fee_fence = []
    for sql, args, digest in fee_scope["query_fence"]:
        require(sql.lstrip().upper().startswith(("SELECT ", "WITH ")), "fee_query_not_readonly")
        reader.rows(sql, args)
        fee_fence.append((sql, tuple(args), digest))
    require(ready.pin_queries(conn, [(sql, args) for sql, args, _ in fee_fence]) == fee_fence, "fee_source_fence_changed")
    fee_sources = _fee_sources(reader, supply_ids, fee_scope)
    first_dispatch = min(e["business_date"] for e in native_plan["effects"] if e["kind"] == "historical_wb_dispatch")
    replay = _replay(reader, book=book, dates=dates, nms=nms, first_dispatch=first_dispatch)
    result = dict(contract=CONTRACT, book_version=fingerprint(book), source_plan_digest=native_plan["plan_digest"],
        book=deepcopy(book), native_plan=deepcopy(native_plan), accounting_capture_digest=accounting_capture["source_digest"],
        native_documents=typed, fee_scope=deepcopy(fee_scope), fee_sources=fee_sources,
        dates=dated, supplies=supplies, cost_layers=costs,
        acceptance_events=replay["acceptance_events"], wb_replay=replay,
        query_fence=json.loads(canonical(ready.pin_queries(conn, reader.queries))))
    require(len(canonical(result).encode("utf-8")) <= MAX_BYTES, "capture_byte_limit")
    result["source_digest"] = fingerprint(result)
    return result


def check_historical_fbo_stage_queries(conn, capture):
    """Recheck exactly the adapter-owned fixed source scopes on the writer."""
    ready.check_queries(conn, [(sql, tuple(args), digest) for sql, args, digest in capture["query_fence"]])
