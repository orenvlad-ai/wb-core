"""Document-scoped fulfillment demand, delivered to the existing warehouse queue.

Source owners capture the matched supplies before commit (including deletion).
The existing FF reconciliation retries only unresolved demand; no history scan.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from packages.application.warehouse_stocks import _normalized_wb_record, _validated_wb_goods

TABLE = "sheet_vitrina_v1_fulfillment_recalc_intents"
QUEUE = "sheet_vitrina_v1_warehouse_targeted_recalc_queue"
UPLOADS = "sheet_vitrina_v1_fulfillment_service_uploads"
LINES = "sheet_vitrina_v1_fulfillment_service_lines"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        upload_id TEXT NOT NULL, source_revision TEXT NOT NULL,
        scope_json TEXT NOT NULL, status TEXT NOT NULL, requested_at TEXT NOT NULL,
        queue_id TEXT, error TEXT, PRIMARY KEY(upload_id,source_revision)
    )""")


def source_revision(upload: Any) -> str:
    return ("deleted:" + str(upload["deleted_at"])) if upload["deleted_at"] else str(upload["file_sha256"])


def load_supply_in_connection(conn: sqlite3.Connection, identity: str) -> sqlite3.Row | None:
    """Use the existing runtime lookup identities without leaving the transaction."""
    values = sorted({identity, f"supply:{identity}", identity.removeprefix("supply:"), identity.removeprefix("preorder:")})
    placeholders = ",".join("?" for _ in values)
    return conn.execute(f"""SELECT * FROM sheet_vitrina_v1_wb_supplies
        WHERE supply_id IN ({placeholders}) OR cache_key IN ({placeholders})
           OR wb_supply_id IN ({placeholders}) OR preorder_id IN ({placeholders})
        ORDER BY CASE WHEN cache_key=? THEN 0 ELSE 1 END,supply_id LIMIT 1""",
        (*values, *values, *values, *values, identity)).fetchone()


def _cost_eligible(row: Any) -> bool | None:
    from packages.application.our_wb_costs import OUR_WB_COST_OPENING_DATE
    # Exactly the existing materializer's COALESCE/date eligibility, not an
    # invitation to materialize earlier history for a new fulfillment upload.
    day = next((row[key] for key in ("supply_date", "fact_date", "updated_date") if row[key] is not None), "")
    try:
        date.fromisoformat(str(day)[:10])
    except ValueError:
        return None  # Missing/invalid dates are unresolved, not known old history.
    return str(day) >= OUR_WB_COST_OPENING_DATE


def _resolve_scope(conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
    for identity, saved in scope.items():
        # A deleted cache row must not erase the previously captured scope.
        row = load_supply_in_connection(conn, identity)
        if row is not None:
            normalized = _normalized_wb_record(dict(row))
            try:
                goods = _validated_wb_goods(normalized)
            except ValueError:
                goods = []
            saved["nm_ids"] = sorted(set(saved["nm_ids"]) | {int(item["nm_id"]) for item in goods})
            saved["supply_id"] = str(row["supply_id"])
            saved["cost_eligible"] = True if saved.get("cost_eligible") is True else _cost_eligible(row)
            dates = [str(normalized.get(field) or "")[:10] for field in ("fact_date", "supply_date", "updated_date", "created_date")]
            day = next((day for day in dates if len(day) == 10), "")
            saved["effective_date"] = min(filter(None, (saved["effective_date"], day)), default="")
    return scope


def capture_request(conn: sqlite3.Connection, upload_id: str, *, requested_at: str) -> dict[str, Any]:
    """Called inside the primary document transaction, after its source update."""
    upload = conn.execute(f"SELECT * FROM {UPLOADS} WHERE upload_id=?", (upload_id,)).fetchone()
    if upload["validation_status"] != "ok":
        return {"status": "not_eligible", "reason": "fulfillment_document_not_confirmed"}
    revision = source_revision(upload)
    existing = conn.execute(f"SELECT 1 FROM {TABLE} WHERE upload_id=? AND source_revision=?", (upload_id, revision)).fetchone()
    if existing is None:
        scope: dict[str, Any] = {}
        for prior in conn.execute(f"SELECT scope_json FROM {TABLE} WHERE upload_id=?", (upload_id,)):
            for identity, saved in json.loads(prior["scope_json"]).items():
                target = scope.setdefault(identity, dict(saved))
                target["nm_ids"] = sorted(set(target["nm_ids"]) | set(saved["nm_ids"]))
                target["effective_date"] = min(filter(None, (target["effective_date"], saved["effective_date"])), default="")
        for line in conn.execute(f"SELECT * FROM {LINES} WHERE upload_id=? AND match_status='ok' AND is_storage_line=0", (upload_id,)):
            identity = str(line["matched_wb_cache_key"] or line["matched_wb_supply_id"] or line["supply_id_input"])
            scope.setdefault(identity, {"nm_ids": [], "effective_date": ""})
        scope = _resolve_scope(conn, scope)
        conn.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,'pending',?,NULL,NULL)", (upload_id, revision, _json(scope), requested_at))
    return deliver_request(conn, upload_id, revision)


def deliver_request(conn: sqlite3.Connection, upload_id: str, revision: str) -> dict[str, Any]:
    """No schema calls, separate connection or commit inside the source transaction."""
    from packages.application.warehouse_functional import enqueue_source_replay_in_connection
    request = dict(conn.execute(f"SELECT * FROM {TABLE} WHERE upload_id=? AND source_revision=?", (upload_id, revision)).fetchone())
    if request["status"] in {"delivered", "not_applicable"}:
        return read_request(conn, upload_id, revision)
    scope = _resolve_scope(conn, json.loads(request["scope_json"]))
    eligible = {identity: saved for identity, saved in scope.items() if saved.get("cost_eligible") is not False}
    missing = [identity for identity, saved in eligible.items() if not saved["nm_ids"] or not saved["effective_date"]]
    if scope and not eligible:
        conn.execute(f"UPDATE {TABLE} SET scope_json=?,status='not_applicable',error='fulfillment_supply_outside_current_cost_window' WHERE upload_id=? AND source_revision=?",
            (_json(scope), upload_id, revision))
    elif not scope or missing:
        conn.execute(f"UPDATE {TABLE} SET scope_json=?,error=? WHERE upload_id=? AND source_revision=?",
            (_json(scope), "matched_supply_scope_pending:" + ",".join(missing or ["missing_matched_lines"]), upload_id, revision))
    else:
        queue = enqueue_source_replay_in_connection(conn,
            stable_source_id="fulfillment_upload:" + upload_id, source_revision=revision,
            effective_date=min(saved["effective_date"] for saved in eligible.values()),
            affected_nm_ids_json=_json(sorted({nm for saved in eligible.values() for nm in saved["nm_ids"]})),
            requested_at=request["requested_at"])
        conn.execute(f"UPDATE {TABLE} SET scope_json=?,status='delivered',queue_id=?,error=NULL WHERE upload_id=? AND source_revision=?",
            (_json(scope), queue["queue_id"], upload_id, revision))
    return read_request(conn, upload_id, revision)


def read_request(conn: sqlite3.Connection, upload_id: str, revision: str) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return {"status": "not_tracked", "reason": "document_predates_atomic_delivery"}
    request = conn.execute(f"SELECT * FROM {TABLE} WHERE upload_id=? AND source_revision=?", (upload_id, revision)).fetchone()
    if request is None:
        return {"status": "not_tracked", "reason": "document_predates_atomic_delivery"}
    identity = {"stable_source_id": "fulfillment_upload:" + upload_id, "source_revision": revision}
    if request["status"] == "not_applicable":
        return {**identity, "status": "no_op", "terminal_no_op": True, "durable_saved": True,
            "reason": request["error"], "warehouse_mutation_count": 0}
    if request["queue_id"]:
        queued = conn.execute(f"SELECT * FROM {QUEUE} WHERE queue_id=? AND stable_source_id=? AND source_revision=?",
            (request["queue_id"], identity["stable_source_id"], revision)).fetchone()
        if queued is not None:
            return {**dict(queued), "durable_saved": True}
    return {**identity, "status": "pending", "durable_saved": True,
        "reason": request["error"] or "fulfillment_queue_delivery_pending"}


def drain_fulfillment_recalc_intents(runtime: Any) -> dict[str, Any]:
    from packages.application.sqlite_contention import connect_sqlite
    with connect_sqlite(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
            return {"status": "no_op", "requests": []}
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(f"SELECT upload_id,source_revision FROM {TABLE} WHERE status='pending' ORDER BY requested_at,upload_id").fetchall()
        results = [deliver_request(conn, row["upload_id"], row["source_revision"]) for row in pending]
        conn.commit()
    return {"status": "pending" if any(row["status"] == "pending" for row in results) else "ok", "requests": results}


def require_current_cost_layers(conn: sqlite3.Connection) -> None:
    """Do not acknowledge a fulfillment revision against an earlier cost overlay.

    This runs inside the warehouse's coherent source capture. An upload racing
    after materialization remains queued until the next ordinary materialization;
    a source change after capture is rejected by the existing warehouse CAS.
    """
    from math import isclose
    from packages.application.fulfillment_services import FulfillmentServicesBlock
    queued = conn.execute(f"SELECT stable_source_id,source_revision FROM {QUEUE} WHERE status IN ('queued','running') AND stable_source_id LIKE 'fulfillment_upload:%'").fetchall()
    if not queued:
        return
    scopes: set[str] = set()
    for row in queued:
        upload_id = str(row["stable_source_id"])[len("fulfillment_upload:"):]
        saved_scope = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            request = conn.execute(f"SELECT scope_json FROM {TABLE} WHERE upload_id=? AND source_revision=?", (upload_id, row["source_revision"])).fetchone()
            saved_scope = json.loads(request["scope_json"]) if request else {}
        for line in conn.execute(f"SELECT matched_wb_cache_key,matched_wb_supply_id,supply_id_input FROM {LINES} WHERE upload_id=? AND match_status='ok' AND is_storage_line=0", (upload_id,)):
            identity = str(line["matched_wb_cache_key"] or line["matched_wb_supply_id"] or line["supply_id_input"])
            supply = load_supply_in_connection(conn, identity)
            if supply is not None:
                current_layer = conn.execute("SELECT 1 FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? AND is_current=1 LIMIT 1", (supply["supply_id"],)).fetchone()
                if _cost_eligible(supply) is not False or current_layer is not None:
                    scopes.add(str(supply["supply_id"]))
            else:
                # Retain the actual layer identity if the source cache vanished
                # after capture; a display/cache prefix is not a cost-layer ID.
                scopes.add(str(saved_scope.get(identity, {}).get("supply_id") or line["matched_wb_supply_id"] or identity.removeprefix("supply:")))
    overlays = FulfillmentServicesBlock.approved_overlay_in_connection(conn)
    for supply_id in sorted(scopes):
        overlay = overlays.get(supply_id) or {}
        expected_ids = set(overlay.get("upload_ids") or [])
        rows = conn.execute("SELECT ff_upload_id,ff_services_amount_total,ff_storage_amount_total FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? AND is_current=1", (supply_id,)).fetchall()
        if not rows:
            raise ValueError("fulfillment_cost_materialization_pending:" + supply_id)
        for row in rows:
            actual_ids = set(filter(None, str(row["ff_upload_id"] or "").split(",")))
            if actual_ids != expected_ids or not isclose(float(row["ff_services_amount_total"]), float(overlay.get("service_amount_with_vat_without_storage_total") or 0), rel_tol=0, abs_tol=1e-8) or not isclose(float(row["ff_storage_amount_total"]), float(overlay.get("storage_allocated_amount_with_vat_total") or 0), rel_tol=0, abs_tol=1e-8):
                raise ValueError("fulfillment_cost_materialization_pending:" + supply_id)
