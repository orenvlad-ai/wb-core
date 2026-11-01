"""Atomic bounded invoice revisions, including evidenced pre-dispatch history.

Run on the explicitly selected runtime through production_apply_launcher.
Source documents and payment identities are never recreated.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import shutil
import sqlite3
import unicodedata

from apps.production_apply_contract import AdapterError
from packages.application.storage_registry import StoreRegistry

PREFIX = "sheet_vitrina_v1_"
AUDIT = PREFIX + "supplier_invoice_revisions"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(encoded(value)).hexdigest()


def require(condition, reason):
    if not condition:
        raise AdapterError(reason)


def connect(path, *, readonly=True):
    require(path.is_file(), "runtime-database-missing")
    if readonly:
        require(Path(str(path) + "-shm").exists() or not Path(str(path) + "-wal").exists(), "readonly-shm-missing")
    conn = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def rows(conn, table, where, values):
    return sorted([dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {where}", values)], key=encoded)


def historical_guards(conn, shipment_id):
    """Read only this shipment's material boundaries, never same-SKU activity.

    Lazy optional tables are represented as absent, rather than a zero count.
    An absent table cannot contain a persisted boundary; no schema is created.
    """
    base_key = "supplier_shipment_acceptance:" + shipment_id
    recovery_prefix = "supplier_shipment_acceptance_recovery:" + shipment_id + ":"
    queries = (
        ("ff_stock_operations", "source_object_id=? OR source_key=? OR substr(source_key,1,?)=?", (shipment_id, base_key, len(recovery_prefix), recovery_prefix)),
        ("supplier_ff_cost_layers", "supplier_shipment_id=?", (shipment_id,)),
        ("supplier_ff_cost_layer_lines", "supplier_shipment_id=?", (shipment_id,)),
        ("ff_guided_acceptance_replays", "shipment_id=?", (shipment_id,)),
        ("ff_pool_documents", "source_id=? AND document_kind='china_acceptance'", (shipment_id,)),
        ("warehouse_business_operations", "source_id=? AND source_type IN ('supplier_shipment','supplier_shipment_acceptance')", (shipment_id,)),
        ("own_capital_events", "shipment_id=? AND event_type NOT IN ('supplier_payment','cost_payment')", (shipment_id,)),
        ("own_capital_expense_certifications", "shipment_id=? AND expenses_complete<>0", (shipment_id,)),
    )
    result = {}
    for table, where, values in queries:
        name = PREFIX + table
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        result[table] = conn.execute(f"SELECT count(*) FROM {name} WHERE {where}", values).fetchone()[0] if present else None
    return result


def snapshot(conn, shipment_id, *, historical=False):
    headers = rows(conn, PREFIX + "supplier_shipments", "shipment_id=?", (shipment_id,))
    require(len(headers) == 1, "shipment-not-found")
    header = headers[0]
    document_id = header["invoice_document_id"]
    docs = rows(conn, PREFIX + "trade_documents", "document_id=?", (document_id,))
    require(len(docs) == 1, "invoice-document-not-found")
    protected = {}
    for table, column in (
        ("supplier_financial_documents", "supplier_order_id"),
        ("supplier_financial_expense_lines", "supplier_order_id"),
        ("cny_documents", "source_order_id"),
        ("cny_ledger_operations", "source_order_id"),
    ):
        where = column + "=?"
        values = (shipment_id,)
        if table == "cny_documents":
            where += " OR context_order_id=?"
            values += (shipment_id,)
        protected[table] = rows(conn, PREFIX + table, where, values)
    protected["contract_links"] = rows(conn, PREFIX + "invoice_contract_links", "invoice_document_id=?", (document_id,))
    for link in protected["contract_links"]:
        protected["contract:" + link["contract_document_id"]] = rows(conn, PREFIX + "trade_documents", "document_id=?", (link["contract_document_id"],))
    result = {"header": header, "lines": rows(conn, PREFIX + "supplier_shipment_lines", "shipment_id=?", (shipment_id,)), "document": docs[0], "protected": protected}
    if historical:
        result["historical_guards"] = historical_guards(conn, shipment_id)
    return result


def trusted_actor():
    # The bounded CLI has no authenticated web actor. Record its effective OS
    # principal; a request must never supply or override this attribution.
    return f"{pwd.getpwuid(os.geteuid()).pw_name}@{socket.gethostname()}:{os.geteuid()}"


def business_today():
    from packages.application.supplier_shipment_status import supplier_business_today
    return date.fromisoformat(supplier_business_today(now=datetime.now(timezone.utc)))


def historical_context(before, meta, supplied):
    require(isinstance(supplied, dict) and set(supplied) == {"source_date", "effective_date", "dispatch_date", "reason", "evidence"}, "invalid-historical-revision-context")
    context = {}
    for key in ("source_date", "effective_date", "dispatch_date"):
        value = supplied[key]
        require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "invalid-historical-revision-date:" + key)
        try:
            date.fromisoformat(value)
        except ValueError:
            require(False, "invalid-historical-revision-date:" + key)
        context[key] = value
    for key in ("reason", "evidence"):
        require(isinstance(supplied[key], str) and 0 < len(supplied[key].strip()) <= 4000, "missing-historical-revision-evidence:" + key)
        context[key] = supplied[key].strip()
    require(str(meta.get("invoice_date") or "") <= context["effective_date"] <= context["source_date"] <= context["dispatch_date"] <= business_today().isoformat(), "historical-revision-date-order-mismatch")
    header = before["header"]
    require(not header.get("actual_shipment_date") or header["actual_shipment_date"] == context["dispatch_date"], "historical-dispatch-date-mismatch")
    require(not header.get("historical_status_exception") and header.get("order_status") not in {"accepted_ff", "archived"}, "historical-revision-requires-unreceived-order")
    guards = before.get("historical_guards")
    require(isinstance(guards, dict) and bool(guards) and all(value is None or value == 0 for value in guards.values()), "historical-revision-material-boundary-exists")
    return {**context, "actor": trusted_actor(), "recorded_actual_shipment_date": header.get("actual_shipment_date"), "factual_date_correction_required": not bool(header.get("actual_shipment_date"))}


def line_key(line):
    if line["line_type"] == "product":
        require(bool(line.get("barcode")), "missing-barcode")
        return "product:" + line["barcode"]
    return "extra:" + re.sub(r"\s+", "", unicodedata.normalize("NFKC", line["model_raw"])).casefold()


def candidate(before, parsed, source, source_sha, *, removed_barcodes=(), historical_pre_dispatch=None):
    header = before["header"]
    require(not header.get("archived_at") and not header.get("actual_ff_acceptance_date"), "revision-requires-unshipped-active-order")
    if historical_pre_dispatch is None:
        require(not header.get("actual_shipment_date"), "revision-requires-unshipped-active-order")
    require(not header.get("expenses_complete"), "certified-order-requires-separate-correction")
    meta = parsed["metadata"]
    require(not parsed["errors"] and not parsed["summary"]["checksum_error"], "invalid-invoice-totals")
    require(meta["invoice_no"] == header["invoice_no"], "invoice-number-mismatch")
    for key in ("contract_no", "contract_date", "currency"):
        require(meta.get(key) == header.get(key), "invoice-identity-mismatch:" + key)
    require(bool(meta.get("invoice_date")), "invoice-date-missing")
    historical = historical_context(before, meta, historical_pre_dispatch) if historical_pre_dispatch is not None else None
    old = {line_key(x): x for x in before["lines"]}
    new = {line_key(x): x for x in parsed["lines"]}
    require(len(old) == len(before["lines"]) and len(new) == len(parsed["lines"]), "assortment-mismatch-or-duplicate")
    require(isinstance(removed_barcodes, (list, tuple)) and all(isinstance(x, str) and x for x in removed_barcodes), "invalid-removed-barcodes")
    require(len(set(removed_barcodes)) == len(removed_barcodes), "duplicate-removed-barcode")
    removed_keys = {"product:" + x for x in removed_barcodes}
    require(set(old) - set(new) == removed_keys and not set(new) - set(old), "assortment-mismatch-or-duplicate")
    require(any(x["line_type"] == "product" for x in new.values()), "revision-requires-retained-products")
    removed_ids = [old[key]["line_id"] for key in sorted(removed_keys)]
    changes = []
    for key, item in old.items():
        if key in removed_keys:
            continue
        replacement = new[key]
        require(item["unit_price"] == replacement["unit_price"], "unit-price-change-not-supported")
        if replacement["line_type"] == "product":
            q = Decimal(str(replacement["qty"]))
            require(q > 0 and q == q.to_integral_value() and bool(item.get("internal_nm_id")), "invalid-product-identity-or-quantity")
        if replacement["qty"] is not None and replacement["unit_price"] is not None:
            amount = Decimal(str(replacement["qty"])) * Decimal(str(replacement["unit_price"]))
            require(abs(amount - Decimal(str(replacement["amount"]))) < Decimal("0.005"), "line-amount-mismatch")
        updates = {k: replacement[k] for k in ("qty", "unit_price", "amount", "model_raw", "model_normalized", "comment", "source_no")}
        updates["raw_json"] = encoded(replacement["raw"]).decode()
        changes.append({"line_id": item["line_id"], "updates": updates})
    relative = str(Path("supplier_invoices/files") / header["shipment_id"] / "revisions" / source_sha / source.name)
    summary = parsed["summary"]
    header_updates = {k: summary[k] for k in ("product_qty_total", "product_amount_total", "extras_amount_total", "invoice_amount_total", "declared_invoice_total")}
    header_updates.update(invoice_date=meta["invoice_date"], source_filename=source.name, source_file_path=relative, source_file_sha256=source_sha, parser_version=parsed["parser_version"])
    document_updates = dict(document_date=meta["invoice_date"], amount_total=summary["invoice_amount_total"], file_original_name=source.name, file_path=relative, file_sha256=source_sha, parser_version=parsed["parser_version"], parsed_metadata_json=encoded({**meta, "supplier_name": header["supplier_name"], "invoice_amount_total": summary["invoice_amount_total"]}).decode())
    result = {"header": header_updates, "lines": changes, "removed_line_ids": removed_ids, "document": document_updates, "file_sha256": source_sha, "file_path": relative}
    if historical is not None:
        result["historical_pre_dispatch"] = historical
    return result


def update(conn, table, id_column, identity, fields):
    count = conn.execute(f"UPDATE {table} SET " + ",".join(k + "=?" for k in fields) + f" WHERE {id_column}=?", (*fields.values(), identity)).rowcount
    require(count == 1, "update-target-count-mismatch")


class SupplierInvoiceRevisionAdapter:
    def context(self, request):
        root = Path(request["runtime_dir"]).resolve()
        require(socket.gethostname() == request["hostname"], "hostname-mismatch")
        marker = Path(request["runtime_sha_file"])
        require(marker.read_text().strip() == request["runtime_sha"], "runtime-sha-mismatch")
        registry = StoreRegistry(root)
        return root, registry.resolve("operational")

    def plan(self, request):
        from packages.application.supplier_invoice_parser import parse_supplier_invoice_xlsx

        root, db = self.context(request)
        source = Path(request["source_path"]).resolve()
        require(source.is_relative_to(root) and source.suffix.lower() == ".xlsx", "source-outside-runtime")
        content = source.read_bytes()
        require(len(content) <= 20 * 1024 * 1024, "invoice-too-large")
        sha = hashlib.sha256(content).hexdigest()
        require(sha == request["source_sha256"], "source-sha-mismatch")
        historical = "historical_pre_dispatch" in request
        if historical:
            require(isinstance(request["historical_pre_dispatch"], dict), "invalid-historical-revision-context")
        with closing(connect(db)) as conn:
            conn.execute("BEGIN")
            before = snapshot(conn, request["shipment_id"], historical=historical)
            require(len(rows(conn, PREFIX + "supplier_shipments", "invoice_document_id=? AND archived_at IS NULL", (before["document"]["document_id"],))) == 1, "shared-invoice-document")
        require(before["document"]["status"] == "active" and before["document"]["document_type"] == "invoice", "inactive-invoice-document")
        require(before["header"]["invoice_no"] == request["invoice_no"], "target-invoice-mismatch")
        for path_key, sha_key, row in (("source_file_path", "source_file_sha256", before["header"]), ("file_path", "file_sha256", before["document"])):
            path = (root / row[path_key]).resolve()
            require(path.is_relative_to(root) and hashlib.sha256(path.read_bytes()).hexdigest() == row[sha_key], "previous-source-file-mismatch")
        parsed = parse_supplier_invoice_xlsx(content, filename=source.name)
        return root, db, before, candidate(before, parsed, source, sha, removed_barcodes=request.get("removed_barcodes", ()), historical_pre_dispatch=request.get("historical_pre_dispatch")), content

    def preview(self, request, operation_id):
        root, db, before, after, _ = self.plan(request)
        scope = {"shipment_id": request["shipment_id"], "invoice_no": request["invoice_no"], "before_quantity": before["header"]["product_qty_total"], "after_quantity": after["header"]["product_qty_total"], "before_amount": before["header"]["invoice_amount_total"], "after_amount": after["header"]["invoice_amount_total"]}
        if "historical_pre_dispatch" in after:
            scope["historical_pre_dispatch"] = after["historical_pre_dispatch"]
        return {"operation_id": operation_id, "target": request["hostname"] + ":" + str(db), "scope": scope, "prestate_sha256": digest(before), "candidate_sha256": digest(after), "recovery": {"before_image": str(root / "supplier_invoice_revisions" / operation_id / "before.json"), "original_invoice": before["header"]["source_file_path"], "method": "restore-exact-header-lines-document-from-before-image-then-targeted-replay"}}

    def apply(self, request, operation_id, preview):
        from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
        from packages.application.supplier_preparation_intents import TABLE, begin_source_change, finish_source_change

        root, _ = self.context(request)
        with warehouse_functional_write_lock(root, timeout_seconds=45):
            root, db, before, after, content = self.plan(request)
            require(digest(before) == preview["prestate_sha256"] and digest(after) == preview["candidate_sha256"], "revision-prestate-or-candidate-drift")
            evidence = root / "supplier_invoice_revisions" / operation_id
            destination = root / after["file_path"]
            required = len(content) + len(encoded(before)) * 3 + 65536
            require(required <= 64 * 1024 * 1024 and shutil.disk_usage(root).free > required + 512 * 1024 * 1024, "insufficient-bounded-revision-capacity")
            evidence.mkdir(parents=True, exist_ok=False)
            for path, data in ((evidence / "before.json", encoded(before)), (evidence / "candidate.json", encoded(after))):
                with path.open("xb") as handle:
                    handle.write(data); handle.flush(); os.fsync(handle.fileno())
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                require(hashlib.sha256(destination.read_bytes()).hexdigest() == after["file_sha256"], "revision-file-conflict")
            else:
                with destination.open("xb") as handle:
                    handle.write(content); handle.flush(); os.fsync(handle.fileno())
            for directory in (evidence, evidence.parent, destination.parent, destination.parent.parent):
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with closing(connect(db, readonly=False)) as conn:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    require(self.context(request)[1] == db, "storage-generation-drift")
                    require(digest(snapshot(conn, request["shipment_id"], historical="historical_pre_dispatch" in after)) == digest(before), "transaction-prestate-drift")
                    supplier_before = begin_source_change(conn, [request["shipment_id"]])
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {AUDIT}(operation_id TEXT PRIMARY KEY,shipment_id TEXT NOT NULL,created_at TEXT NOT NULL,prestate_json TEXT NOT NULL,candidate_json TEXT NOT NULL,queue_json TEXT NOT NULL)")
                    require(conn.execute(f"SELECT 1 FROM {AUDIT} WHERE operation_id=?", (operation_id,)).fetchone() is None, "operation-already-submitted")
                    update(conn, PREFIX + "supplier_shipments", "shipment_id", request["shipment_id"], {**after["header"], "updated_at": now})
                    for line_id in after.get("removed_line_ids", []):
                        count = conn.execute(f"DELETE FROM {PREFIX}supplier_shipment_lines WHERE shipment_id=? AND line_id=?", (request["shipment_id"], line_id)).rowcount
                        require(count == 1, "removed-line-count-mismatch")
                    for line in after["lines"]:
                        update(conn, PREFIX + "supplier_shipment_lines", "line_id", line["line_id"], line["updates"])
                    update(conn, PREFIX + "trade_documents", "document_id", before["document"]["document_id"], {**after["document"], "updated_at": now})
                    require(snapshot(conn, request["shipment_id"])["protected"] == before["protected"], "protected-links-changed")
                    finish_source_change(conn, supplier_before)
                    intent = dict(conn.execute(f"SELECT * FROM {TABLE} WHERE shipment_id=?", (request["shipment_id"],)).fetchone())
                    pending = {"status": "pending", "shipment_id": request["shipment_id"], "preparation_revision": intent["revision"], "source_fingerprint": intent["source_fingerprint"], "operation_id": operation_id}
                    conn.execute(f"INSERT INTO {AUDIT} VALUES(?,?,?,?,?,?)", (operation_id, request["shipment_id"], now, encoded(before).decode(), encoded(after).decode(), encoded(pending).decode()))
        # Atomic source+intent+audit are durable before admitted continuation.
        try:
            queue = self.enqueue(root, before, after, now)
            if queue.get("preparation_revision") != pending["preparation_revision"]:
                # A concurrent newer source may have coalesced the demand.
                # Its receipt must not complete this operation's old revision.
                queue = {**pending, "reason": "source_revision_superseded",
                         "latest_preparation_revision": queue.get("preparation_revision")}
        except Exception as exc:
            queue = {**pending, "error": str(exc).replace("\n", " ")[:500]}
        try:
            with closing(connect(db, readonly=False)) as conn:
                with conn:
                    update(conn, AUDIT, "operation_id", operation_id, {"queue_json": encoded(queue).decode()})
        except Exception as exc:
            # The atomic audit already records applied source + its durable
            # pending identity. A late receipt failure cannot undo that fact.
            queue = {**queue, "audit_receipt_pending": True, "receipt_error": str(exc).replace("\n", " ")[:500]}
        return {"operation_id": operation_id, "disposition": "submitted", "queue": queue}

    def enqueue(self, root, before, after, now):
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.supplier_preparation_intents import resume_supplier_preparation

        # Include the original invoice/payment boundary when the revised document is dated later.
        return resume_supplier_preparation(RegistryUploadDbBackedRuntime(runtime_dir=root), before["header"]["shipment_id"])

    def readback(self, request, operation_id):
        root, db = self.context(request)
        with closing(connect(db)) as conn:
            conn.execute("BEGIN")
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (AUDIT,)).fetchone()
            audit = conn.execute(f"SELECT * FROM {AUDIT} WHERE operation_id=?", (operation_id,)).fetchone() if exists else None
            if audit is None:
                return {"operation_id": operation_id, "state": "not_submitted"}
            before = json.loads(audit["prestate_json"])
            after = json.loads(audit["candidate_json"])
            require(audit["shipment_id"] == request["shipment_id"], "readback-shipment-mismatch")
            current = snapshot(conn, request["shipment_id"], historical="historical_pre_dispatch" in after)
        checks = [current["protected"] == before["protected"], hashlib.sha256((root / after["file_path"]).read_bytes()).hexdigest() == after["file_sha256"]]
        if "historical_pre_dispatch" in after:
            # Native continuation may create an empty lazy schema. It must not
            # create a material boundary for this bounded source correction.
            checks.append(all(value is None or value == 0 for value in current["historical_guards"].values()))
        for section in ("header", "document"):
            checks.append(all(current[section][k] == v for k, v in after[section].items()))
            checks.append(all(current[section][k] == v for k, v in before[section].items() if k not in after[section] and k != "updated_at"))
        by_id = {x["line_id"]: x for x in current["lines"]}
        checks.append(set(by_id) == {x["line_id"] for x in before["lines"]} - set(after.get("removed_line_ids", [])))
        for line in after["lines"]:
            checks.append(all(by_id.get(line["line_id"], {}).get(k) == v for k, v in line["updates"].items()))
            previous = next(x for x in before["lines"] if x["line_id"] == line["line_id"])
            checks.append(all(by_id.get(line["line_id"], {}).get(k) == v for k, v in previous.items() if k not in line["updates"]))
        queue = json.loads(audit["queue_json"])
        return {"operation_id": operation_id, "state": "applied" if all(checks) and queue else "failed", "source_verified": all(checks), "queue": queue, "recovery_reference": str(root / "supplier_invoice_revisions" / operation_id / "before.json")}
