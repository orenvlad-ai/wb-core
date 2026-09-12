"""Source-owned supplier continuation, consumed by the existing warehouse worker.

The row coalesces demand, never source documents. Its revision is monotonic even
when a source returns to an earlier value. No historical source scan is used.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterable

TABLE = "sheet_vitrina_v1_supplier_preparation_intents"
PREFIX = "sheet_vitrina_v1_"
HEADER_FIELDS = (
    "shipment_id", "invoice_no", "invoice_date", "currency", "shipment_date",
    "actual_shipment_date", "actual_ff_acceptance_date", "order_status",
    "target_facility_id", "expenses_complete", "approx_yuan_rate",
    "declared_invoice_total", "invoice_amount_total", "match_status", "archived_at",
)
LINE_FIELDS = (
    "line_id", "line_type", "internal_nm_id", "internal_sku", "barcode",
    "qty", "unit_price", "amount", "currency", "product_type", "match_key",
)
DOCUMENT_FIELDS = (
    "document_id", "supplier_order_id", "document_type", "parse_status",
    "document_date", "currency", "total_amount", "total_amount_rub",
    "vat_rate", "vat_amount_rub", "cbr_usd_rate_value", "normalized_parse_json",
)
EXPENSE_FIELDS = (
    "line_id", "financial_document_id", "supplier_order_id", "sort_order",
    "category", "stage", "description", "amount", "currency", "amount_rub",
    "vat_rate", "vat_amount_rub", "included_in_logistics_efficiency",
    "included_in_customs_total", "status", "confidence", "raw_json",
)
COST_DOCUMENT_TYPES = {"logistics_invoice", "customs_declaration", "bank_transfer_application", "bank_fee_statement"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_schema(conn: sqlite3.Connection) -> None:
    # execute (not executescript): never commits the caller's source transaction.
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        shipment_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
        source_fingerprint TEXT NOT NULL, affected_nm_ids_json TEXT NOT NULL,
        effective_date TEXT NOT NULL, document_ids_json TEXT NOT NULL,
        status TEXT NOT NULL, requested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        prepared_at TEXT, queue_id TEXT, error TEXT,
        post_actions_json TEXT NOT NULL DEFAULT '{{}}', costs_required INTEGER NOT NULL DEFAULT 1
    )""")
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    for name, definition in (("post_actions_json", "TEXT NOT NULL DEFAULT '{}'"), ("costs_required", "INTEGER NOT NULL DEFAULT 1")):
        if name not in columns:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {definition}")


def _selected(row: Any, fields: Iterable[str]) -> dict[str, Any]:
    data = dict(row or {})
    return {key: json.loads(data[key]) if key.endswith("_json") and data.get(key) else data.get(key) for key in fields}


def capture_source(conn: sqlite3.Connection, shipment_id: str) -> dict[str, Any]:
    header = conn.execute(f"SELECT * FROM {PREFIX}supplier_shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    lines = conn.execute(f"SELECT * FROM {PREFIX}supplier_shipment_lines WHERE shipment_id=? ORDER BY line_id", (shipment_id,)).fetchall()
    documents = conn.execute(f"SELECT * FROM {PREFIX}supplier_financial_documents WHERE supplier_order_id=? ORDER BY document_id", (shipment_id,)).fetchall()
    expenses = conn.execute(f"SELECT * FROM {PREFIX}supplier_financial_expense_lines WHERE supplier_order_id=? ORDER BY line_id", (shipment_id,)).fetchall()
    return {
        "header": _selected(header, HEADER_FIELDS) if header is not None else {},
        "lines": [_selected(row, LINE_FIELDS) for row in lines],
        "documents": [_selected(row, DOCUMENT_FIELDS) for row in documents if row["document_type"] in COST_DOCUMENT_TYPES],
        "expenses": [_selected(row, EXPENSE_FIELDS) for row in expenses if row["financial_document_id"] in {doc["document_id"] for doc in documents if doc["document_type"] in COST_DOCUMENT_TYPES}],
    }


def begin_source_change(conn: sqlite3.Connection, shipment_ids: Iterable[str]) -> dict[str, Any]:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    return {key: capture_source(conn, key) for key in sorted(set(shipment_ids)) if key}


def finish_source_change(conn: sqlite3.Connection, before: dict[str, Any], *, reset_expenses: bool = False, post_actions: dict[str, Any] | None = None) -> None:
    for shipment_id, previous in before.items():
        current = capture_source(conn, shipment_id)
        source_changed = current != previous
        if not source_changed and not post_actions:
            continue
        if reset_expenses and (current["documents"] != previous["documents"] or current["expenses"] != previous["expenses"]):
            conn.execute(f"UPDATE {PREFIX}supplier_shipments SET expenses_complete=0 WHERE shipment_id=?", (shipment_id,))
            current = capture_source(conn, shipment_id)
        ensure_schema(conn)
        existing = conn.execute(f"SELECT * FROM {TABLE} WHERE shipment_id=?", (shipment_id,)).fetchone()
        pending = dict(existing) if existing is not None and existing["status"] != "delivered" else {}
        actions = {**json.loads(pending.get("post_actions_json", "{}")), **(post_actions or {})}
        if "invoice_archive" in actions:
            # The newer archive supersedes an unfinished link edit. Linking
            # after archiving would require an invoice that is no longer active.
            actions.pop("invoice_contract", None)
        ids = set(json.loads(pending.get("affected_nm_ids_json", "[]")))
        docs = set(json.loads(pending.get("document_ids_json", "[]")))
        dates = [pending.get("effective_date", "")]
        for state in (previous, current):
            ids.update(int(line["internal_nm_id"]) for line in state["lines"] if line.get("line_type") == "product" and int(line.get("internal_nm_id") or 0) > 0)
            dates.extend(str(state["header"].get(key) or "")[:10] for key in ("invoice_date", "shipment_date", "actual_shipment_date", "actual_ff_acceptance_date"))
            dates.extend(str(doc.get("document_date") or "")[:10] for doc in state["documents"])
        old_documents = {doc["document_id"]: doc for doc in previous["documents"]}
        new_documents = {doc["document_id"]: doc for doc in current["documents"]}
        docs.update(key for key in old_documents.keys() | new_documents.keys() if old_documents.get(key) != new_documents.get(key))
        if previous["lines"] != current["lines"] or any(previous["header"].get(key) != current["header"].get(key) for key in ("invoice_date", "actual_shipment_date", "actual_ff_acceptance_date", "archived_at")):
            docs.update(old_documents.keys() | new_documents.keys())
        if previous["expenses"] != current["expenses"]:
            docs.update(row["financial_document_id"] for row in previous["expenses"] + current["expenses"])
        # Payment dates can precede an invoice correction. Capture only this
        # shipment's source boundary; never use all shipments as empty fallback.
        dates.extend(str(row[0] or "")[:10] for row in conn.execute(f"SELECT operation_date FROM {PREFIX}cny_documents WHERE source_order_id=?", (shipment_id,)))
        now = _now()
        conn.execute(f"""INSERT INTO {TABLE}(shipment_id,revision,source_fingerprint,affected_nm_ids_json,effective_date,document_ids_json,status,requested_at,updated_at,prepared_at,queue_id,error,post_actions_json,costs_required)
            VALUES(?,?,?,?,?,?,'pending',?,?,NULL,NULL,NULL,?,?)
            ON CONFLICT(shipment_id) DO UPDATE SET
                revision=excluded.revision,source_fingerprint=excluded.source_fingerprint,
                affected_nm_ids_json=excluded.affected_nm_ids_json,effective_date=excluded.effective_date,
                document_ids_json=excluded.document_ids_json,status='pending',
                requested_at=excluded.requested_at,updated_at=excluded.updated_at,
                prepared_at=NULL,queue_id=NULL,error=NULL,
                post_actions_json=excluded.post_actions_json,costs_required=excluded.costs_required""", (
            shipment_id, int(existing["revision"] if existing is not None else 0) + 1,
            _fingerprint(current), _json(sorted(ids)), min((day for day in dates if day), default=""),
            _json(sorted(docs)), pending.get("requested_at") or now, now,
            _json(actions), int(source_changed or bool(pending.get("costs_required"))),
        ))


def ensure_explicit_document_continuation(runtime: Any, shipment_id: str, document_id: str) -> None:
    """Admit one explicitly requested legacy source; never scan historical sources."""
    from packages.application.registry_upload_db_backed_runtime import _connect

    with _connect(runtime.db_path) as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(f"SELECT 1 FROM {TABLE} WHERE shipment_id=?", (shipment_id,)).fetchone():
            return
        current = capture_source(conn, shipment_id)
        document = next((doc for doc in current["documents"] if doc["document_id"] == document_id), None)
        if not current["header"] or document is None:
            raise ValueError("explicit supplier continuation has no saved cost source")
        previous = {**current, "documents": [doc for doc in current["documents"] if doc["document_id"] != document_id],
                    "expenses": [row for row in current["expenses"] if row["financial_document_id"] != document_id]}
        finish_source_change(conn, {shipment_id: previous})
        conn.commit()


def _prepare_post_actions(runtime: Any, request: dict[str, Any]) -> None:
    from packages.application.supplier_shipments import SupplierShipmentsBlock

    for kind, action in json.loads(request["post_actions_json"]).items():
        shipment = runtime.load_supplier_shipment(request["shipment_id"])
        invoice_id = str(action.get("invoice_document_id") or "")
        if shipment is None or not invoice_id or shipment["header"].get("invoice_document_id") != invoice_id:
            raise ValueError("supplier invoice link continuation has no exact invoice source")
        block = SupplierShipmentsBlock(runtime=runtime)
        if kind == "invoice_archive":
            invoice = runtime.load_trade_document(invoice_id)
            if invoice is None or invoice.get("status") != "archived":
                runtime.archive_trade_document(invoice_id, updated_at=str(action["updated_at"]))
        elif kind == "invoice_contract":
            existing = runtime.load_invoice_contract_link(invoice_id)
            if action.get("contract_document_id"):
                if existing is None or existing["contract_document_id"] != action["contract_document_id"]:
                    block.link_invoice_to_contract(**action)
            elif existing is not None:
                block.unlink_invoice_contract(invoice_id)
        else:
            raise ValueError("unknown supplier post-save continuation")


def _request_source_matches(conn: sqlite3.Connection, request: dict[str, Any]) -> bool:
    if _fingerprint(capture_source(conn, request["shipment_id"])) != request["source_fingerprint"]:
        return False
    actions = json.loads(request["post_actions_json"])
    if actions:
        header = conn.execute(f"SELECT invoice_document_id FROM {PREFIX}supplier_shipments WHERE shipment_id=?", (request["shipment_id"],)).fetchone()
        if header is None or any(action.get("invoice_document_id") != header[0] for action in actions.values()):
            return False
    return True


def _prepare(runtime: Any, request: dict[str, Any]) -> dict[str, Any]:
    from packages.application.own_product_capital import OwnProductCapitalBlock
    from packages.application.supplier_shipments import SupplierShipmentsBlock
    from packages.application.registry_upload_db_backed_runtime import _connect

    shipment_id = request["shipment_id"]
    shipment = runtime.load_supplier_shipment(shipment_id)
    capital = OwnProductCapitalBlock(runtime=runtime)
    if shipment is None:
        raise ValueError("supplier preparation source shipment missing: " + shipment_id)
    header = shipment["header"]
    with _connect(runtime.db_path) as conn:
        certificate = conn.execute(f"SELECT expenses_complete FROM {PREFIX}own_capital_expense_certifications WHERE shipment_id=?", (shipment_id,)).fetchone()
    recalculate = bool(header.get("expenses_complete")) or certificate is not None
    if recalculate:
        certification = capital.set_expenses_certifications(
            shipment_ids=[shipment_id], expenses_complete=bool(header.get("expenses_complete")),
            recalculate=False, preserve_unchanged=True,
        )
        recalculate = bool(certification["changed_shipment_count"])
    cny_replay_required = False
    replay: dict[str, Any] = {}
    archived_cny: list[str] = []
    changed_cny: list[str] = []
    for document_id in json.loads(request["document_ids_json"]):
        document = runtime.load_supplier_financial_document(supplier_order_id=shipment_id, document_id=document_id)
        if document is None:
            with _connect(runtime.db_path) as conn:
                new_owner = conn.execute(f"SELECT supplier_order_id FROM {PREFIX}supplier_financial_documents WHERE document_id=?", (document_id,)).fetchone()
            if new_owner is not None and new_owner[0] != shipment_id:
                # The new owner's intent replaces this document's allocation.
                # An old-owner retry must not delete the new owner's result.
                continue
        remove_expenses = document is None or document.get("parse_status") in {"excluded", "parse_error", "needs_review"}
        if document is not None and not remove_expenses:
            from packages.application.own_product_capital import _expense_event_plans

            plans = _expense_event_plans(document, document.get("expense_lines") or [])
            expected_prefixes = tuple("cost_payment:" + str(plan["event_document_id"]) + ":" for plan in plans)
            source_prefix = "cost_payment:financial_expense:" + document_id + ":"
            with _connect(runtime.db_path) as conn:
                saved_event_ids = [str(row[0]) for row in conn.execute(f"SELECT event_id FROM {PREFIX}own_capital_events WHERE substr(event_id,1,?)=?", (len(source_prefix), source_prefix))]
            # A revision may remove an expense group/date completely. Such an
            # old event will never collide with a surviving plan's event key.
            remove_expenses = any(not event_id.startswith(expected_prefixes) for event_id in saved_event_ids)
        if remove_expenses:
            removed = capital.remove_financial_document_expenses(document_id, recalculate=False)
            recalculate = recalculate or bool(removed["removed_event_count"])
        if document is not None and document.get("document_type") == "bank_fee_statement" and document.get("parse_status") == "confirmed":
            from packages.application.cny_ledger import CnyLedgerBlock
            from packages.application.supplier_financial_documents import _confirmed_cny_fee_rows

            ledger = CnyLedgerBlock(runtime=runtime)
            for fee in _confirmed_cny_fee_rows(document):
                natural_key = str(fee.get("cny_ledger_natural_key") or "")
                if not natural_key:
                    raise ValueError("saved statement fee has no durable natural key")
                if runtime.load_cny_document_by_natural_key(natural_key) is None:
                    ledger.save_bank_fee_document(
                        source_order_id=shipment_id, linked_financial_document_id=document_id,
                        natural_key=natural_key, fee_row=fee,
                        original_filename=str(document.get("original_filename") or ""),
                        stored_file_path=str(document.get("stored_file_path") or ""),
                        file_content_type=str(document.get("file_content_type") or ""), replay=False,
                    )
                cny_replay_required = True
        # Resume only links owned by the saved financial source. This invokes
        # the existing ledger operation; it does not introduce a new replay.
        with _connect(runtime.db_path) as conn:
            linked_ids = [row[0] for row in conn.execute(f"SELECT document_id FROM {PREFIX}cny_documents WHERE linked_financial_document_id=? ORDER BY document_id", (document_id,))]
        for linked_id in linked_ids:
            linked = runtime.load_cny_document(linked_id)
            if linked is None:
                raise ValueError("linked CNY source changed during supplier preparation")
            if document is None or document.get("parse_status") == "excluded":
                target_status = "excluded"
                cny_replay_required = True
                archived_cny.append(str(linked["document_id"]))
            elif document.get("document_type") == "bank_fee_statement":
                target_status = "posted" if document.get("parse_status") == "confirmed" else "excluded"
                cny_replay_required = True
            else:
                continue
            if linked.get("status") != target_status:
                runtime.save_cny_document({**linked, "status": target_status, "updated_at": _now()})
                changed_cny.append(str(linked["document_id"]))
                cny_replay_required = True
    if cny_replay_required:
        from packages.application.cny_ledger import CnyLedgerBlock

        replay = CnyLedgerBlock(runtime=runtime).replay_ledger(reason="supplier_financial_source_preparation")
        if replay.get("status") == "pending":
            raise ValueError("saved supplier statement is awaiting CNY preparation")
    events = capital.materialize_persisted_expense_events(shipment_id=shipment_id, recalculate=False) if json.loads(request["document_ids_json"]) else {"status": "ok", "created_event_group_count": 0, "idempotent_event_group_count": 0}
    changed_documents = set(json.loads(request["document_ids_json"]))
    conflicts = [row for row in events.get("blockers", []) if row.get("code") == "expense_capital_allocation_blocked" and ("conflicts with persisted evidence" in str(row.get("reason") or "") or str(row.get("reason") or "") == "cost payment document already materialized with different allocations")]
    for conflict in conflicts:
        document_id = str(conflict["document_id"])
        if document_id not in changed_documents:
            raise ValueError("expense event conflict outside saved supplier scope")
        # Expense events are derived, keyed by their source document. A revised
        # source must replace its old allocation, including removed SKU/date.
        capital.remove_financial_document_expenses(document_id, recalculate=False)
        recalculate = True
    if conflicts:
        events = capital.materialize_persisted_expense_events(shipment_id=shipment_id, recalculate=False)
        if any(row.get("code") == "expense_capital_allocation_blocked" for row in events.get("blockers", [])):
            raise ValueError("supplier expense preparation remains blocked")
    recalculate = recalculate or bool(events.get("created_event_group_count"))
    if not header.get("archived_at") and header.get("actual_ff_acceptance_date"):
        block = SupplierShipmentsBlock(runtime=runtime)
        block._record_ff_stock_receipt(shipment)
        block._materialize_ff_cost_layer(shipment_id)
        block._reconcile_ff_reservations()
        recalculate = True
    calculated = capital.recalculate(drain_projection=False) if recalculate else None
    return {"cny_ledger_replay": replay, "cny_documents_archived": archived_cny,
            "cny_documents_status_changed": changed_cny,
            "own_product_capital": events, "own_product_capital_preparation": {
        "status": "ok", "daily_rows_changed": calculated.daily_rows_changed if calculated else 0,
        "run_fingerprint": calculated.fingerprint if calculated else None,
        "recalculated": recalculate,
    }}


def drain_supplier_preparation_intents(runtime: Any, *, shipment_ids: Iterable[str] | None = None, inject_failure: Any = None) -> dict[str, Any]:
    """Prepare saved source revisions and hand them to the canonical queue.

    Work is retriable. Acknowledge and queue insertion share a short transaction
    after an exact source/revision/scope recheck. A new revision stays pending.
    """
    from packages.application.registry_upload_db_backed_runtime import _connect
    from packages.application.warehouse_functional import ensure_warehouse_functional_schema, enqueue_supplier_replay_in_connection
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock

    wanted = sorted(set(shipment_ids or []))
    with _connect(runtime.db_path) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
            return {"status": "no_op", "requests": []}
        where = " WHERE status IN ('pending','error')"
        values: list[Any] = []
        if shipment_ids is not None:
            if not wanted:
                return {"status": "no_op", "requests": []}
            where += " AND shipment_id IN (" + ",".join("?" for _ in wanted) + ")"
            values.extend(wanted)
        requests = [dict(row) for row in conn.execute(f"SELECT * FROM {TABLE}" + where + " ORDER BY requested_at,shipment_id", values)]
    results = []
    for request in requests:
        preparation_complete = bool(request["prepared_at"])
        preparation: dict[str, Any] = {}
        try:
            with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=45):
                with _connect(runtime.db_path) as conn:
                    conn.execute("BEGIN")
                    captured = conn.execute(f"SELECT * FROM {TABLE} WHERE shipment_id=?", (request["shipment_id"],)).fetchone()
                    unchanged = captured is not None and dict(captured) == request and _request_source_matches(conn, request)
                if not unchanged:
                    # Another consumer may have delivered this request while
                    # we waited. Do not repeat its domain preparation.
                    if captured is not None and captured["status"] in {"pending", "error"}:
                        results.append({"status": "pending", "shipment_id": request["shipment_id"], "preparation_revision": captured["revision"], "error": "new supplier source revision awaits preparation"})
                    continue
                if not preparation_complete:
                    if inject_failure:
                        inject_failure("before_preparation", request)
                    _prepare_post_actions(runtime, request)
                    preparation = _prepare(runtime, request) if request["costs_required"] else {}
                    preparation_complete = True
                    if inject_failure:
                        inject_failure("after_preparation", request)
                if request["costs_required"] and (not json.loads(request["affected_nm_ids_json"]) or not request["effective_date"]):
                    raise ValueError("supplier preparation has no proven SKU/date scope")
                with _connect(runtime.db_path) as conn:
                    ensure_warehouse_functional_schema(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute(f"SELECT * FROM {TABLE} WHERE shipment_id=?", (request["shipment_id"],)).fetchone()
                    if current is None or dict(current) != request or not _request_source_matches(conn, request):
                        raise ValueError("supplier preparation source revision changed; retry pending revision")
                    queue = enqueue_supplier_replay_in_connection(conn, request=request) if request["costs_required"] else {"status": "complete", "queue_id": None, "reason": "supplier_post_actions_completed"}
                    if inject_failure:
                        inject_failure("before_ack", request)
                    conn.execute(f"UPDATE {TABLE} SET status='delivered',prepared_at=?,queue_id=?,error=NULL WHERE shipment_id=? AND revision=? AND source_fingerprint=?", (_now(), queue["queue_id"], request["shipment_id"], request["revision"], request["source_fingerprint"]))
                    conn.commit()
                results.append({**queue, **preparation, "shipment_id": request["shipment_id"], "preparation_revision": request["revision"]})
        except Exception as exc:
            error = str(exc).replace("\n", " ")[:500]
            with _connect(runtime.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                prepared_at = _now() if preparation_complete and _request_source_matches(conn, request) else request["prepared_at"]
                conn.execute(f"UPDATE {TABLE} SET status='error',error=?,prepared_at=? WHERE shipment_id=? AND revision=? AND source_fingerprint=? AND status<>'delivered'", (error, prepared_at, request["shipment_id"], request["revision"], request["source_fingerprint"]))
                conn.commit()
            results.append({**preparation, "status": "pending", "retryable": not error.startswith("supplier preparation has no proven"), "shipment_id": request["shipment_id"], "preparation_revision": request["revision"], "stable_source_id": "supplier_shipment:" + request["shipment_id"], "source_revision": "supplier-preparation:" + str(request["revision"]) + ":" + request["source_fingerprint"], "affected_nm_ids_json": request["affected_nm_ids_json"], "error": error})
    return {"status": "pending" if any(row["status"] == "pending" for row in results) else "queued" if results else "no_op", "requests": results}


def resume_supplier_preparation(runtime: Any, shipment_id: str) -> dict[str, Any]:
    try:
        return _resume_supplier_preparation(runtime, shipment_id)
    except Exception as exc:
        # Callers invoke this only after the source transaction has succeeded.
        return {"status": "pending", "operation_applied": True, "shipment_id": shipment_id, "error": str(exc).replace("\n", " ")[:500]}


def _resume_supplier_preparation(runtime: Any, shipment_id: str) -> dict[str, Any]:
    result = drain_supplier_preparation_intents(runtime, shipment_ids=[shipment_id])
    requests = result["requests"]
    if requests:
        return requests[0]
    from packages.application.registry_upload_db_backed_runtime import _connect
    with _connect(runtime.db_path) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None:
            row = conn.execute(f"SELECT * FROM {TABLE} WHERE shipment_id=?", (shipment_id,)).fetchone()
            if row is not None:
                if row["status"] == "delivered":
                    if not _request_source_matches(conn, dict(row)):
                        return {"status": "pending", "retryable": False, "shipment_id": shipment_id, "error": "saved source differs from its completed preparation"}
                    if not row["costs_required"]:
                        return {"status": "complete", "shipment_id": shipment_id, "preparation_revision": row["revision"], "reason": "supplier_post_actions_completed"}
                    queued = conn.execute(f"SELECT * FROM {PREFIX}warehouse_targeted_recalc_queue WHERE queue_id=?", (row["queue_id"],)).fetchone()
                    if queued is not None:
                        return {**dict(queued), "preparation_revision": row["revision"], "shipment_id": shipment_id}
                return {"status": "pending", "queue_id": row["queue_id"], "preparation_revision": row["revision"], "shipment_id": shipment_id}
    return {"status": "pending", "retryable": False, "shipment_id": shipment_id, "error": "saved source has no proven continuation; explicit source finalization required"}
