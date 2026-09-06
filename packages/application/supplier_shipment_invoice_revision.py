"""Atomic quantity-only invoice revisions before supplier dispatch/receipt.

Run on the explicitly selected runtime through production_apply_launcher.
Source documents and payment identities are never recreated.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
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


def snapshot(conn, shipment_id):
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
    return {"header": header, "lines": rows(conn, PREFIX + "supplier_shipment_lines", "shipment_id=?", (shipment_id,)), "document": docs[0], "protected": protected}


def line_key(line):
    if line["line_type"] == "product":
        require(bool(line.get("barcode")), "missing-barcode")
        return "product:" + line["barcode"]
    return "extra:" + re.sub(r"\s+", "", unicodedata.normalize("NFKC", line["model_raw"])).casefold()


def candidate(before, parsed, source, source_sha):
    header = before["header"]
    require(not header.get("archived_at") and not header.get("actual_ff_acceptance_date") and not header.get("actual_shipment_date"), "revision-requires-unshipped-active-order")
    require(not header.get("expenses_complete"), "certified-order-requires-separate-correction")
    meta = parsed["metadata"]
    require(not parsed["errors"] and not parsed["summary"]["checksum_error"], "invalid-invoice-totals")
    require(meta["invoice_no"] == header["invoice_no"], "invoice-number-mismatch")
    for key in ("contract_no", "contract_date", "currency"):
        require(meta.get(key) == header.get(key), "invoice-identity-mismatch:" + key)
    require(bool(meta.get("invoice_date")), "invoice-date-missing")
    old = {line_key(x): x for x in before["lines"]}
    new = {line_key(x): x for x in parsed["lines"]}
    require(len(old) == len(before["lines"]) and len(new) == len(parsed["lines"]) and old.keys() == new.keys(), "assortment-mismatch-or-duplicate")
    changes = []
    for key, item in old.items():
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
    return {"header": header_updates, "lines": changes, "document": document_updates, "file_sha256": source_sha, "file_path": relative}


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
        with closing(connect(db)) as conn:
            conn.execute("BEGIN")
            before = snapshot(conn, request["shipment_id"])
            require(len(rows(conn, PREFIX + "supplier_shipments", "invoice_document_id=? AND archived_at IS NULL", (before["document"]["document_id"],))) == 1, "shared-invoice-document")
        require(before["document"]["status"] == "active" and before["document"]["document_type"] == "invoice", "inactive-invoice-document")
        require(before["header"]["invoice_no"] == request["invoice_no"], "target-invoice-mismatch")
        for path_key, sha_key, row in (("source_file_path", "source_file_sha256", before["header"]), ("file_path", "file_sha256", before["document"])):
            path = (root / row[path_key]).resolve()
            require(path.is_relative_to(root) and hashlib.sha256(path.read_bytes()).hexdigest() == row[sha_key], "previous-source-file-mismatch")
        parsed = parse_supplier_invoice_xlsx(content, filename=source.name)
        return root, db, before, candidate(before, parsed, source, sha), content

    def preview(self, request, operation_id):
        root, db, before, after, _ = self.plan(request)
        return {"operation_id": operation_id, "target": request["hostname"] + ":" + str(db), "scope": {"shipment_id": request["shipment_id"], "invoice_no": request["invoice_no"], "before_quantity": before["header"]["product_qty_total"], "after_quantity": after["header"]["product_qty_total"], "before_amount": before["header"]["invoice_amount_total"], "after_amount": after["header"]["invoice_amount_total"]}, "prestate_sha256": digest(before), "candidate_sha256": digest(after), "recovery": {"before_image": str(root / "supplier_invoice_revisions" / operation_id / "before.json"), "original_invoice": before["header"]["source_file_path"], "method": "restore-exact-header-lines-document-from-before-image-then-targeted-replay"}}

    def apply(self, request, operation_id, preview):
        from packages.application.warehouse_functional_lock import warehouse_functional_write_lock

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
                    require(digest(snapshot(conn, request["shipment_id"])) == digest(before), "transaction-prestate-drift")
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {AUDIT}(operation_id TEXT PRIMARY KEY,shipment_id TEXT NOT NULL,created_at TEXT NOT NULL,prestate_json TEXT NOT NULL,candidate_json TEXT NOT NULL,queue_json TEXT NOT NULL)")
                    require(conn.execute(f"SELECT 1 FROM {AUDIT} WHERE operation_id=?", (operation_id,)).fetchone() is None, "operation-already-submitted")
                    update(conn, PREFIX + "supplier_shipments", "shipment_id", request["shipment_id"], {**after["header"], "updated_at": now})
                    for line in after["lines"]:
                        update(conn, PREFIX + "supplier_shipment_lines", "line_id", line["line_id"], line["updates"])
                    update(conn, PREFIX + "trade_documents", "document_id", before["document"]["document_id"], {**after["document"], "updated_at": now})
                    require(snapshot(conn, request["shipment_id"])["protected"] == before["protected"], "protected-links-changed")
                    conn.execute(f"INSERT INTO {AUDIT} VALUES(?,?,?,?,?,?)", (operation_id, request["shipment_id"], now, encoded(before).decode(), encoded(after).decode(), "{}"))
            queue = self.enqueue(root, before, after, now)
            with closing(connect(db, readonly=False)) as conn:
                with conn:
                    update(conn, AUDIT, "operation_id", operation_id, {"queue_json": encoded(queue).decode()})
            return {"operation_id": operation_id, "disposition": "submitted", "queue": queue}

    def enqueue(self, root, before, after, now):
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.warehouse_functional import enqueue_warehouse_targeted_recalculation

        # Include the original invoice/payment boundary when the revised document is dated later.
        dates = [before["header"]["invoice_date"], after["header"]["invoice_date"]]
        dates += [x.get("operation_date") for x in before["protected"]["cny_documents"]]
        return enqueue_warehouse_targeted_recalculation(runtime=RegistryUploadDbBackedRuntime(runtime_dir=root), stable_source_id="supplier_shipment:" + before["header"]["shipment_id"], source_revision=digest(after), effective_date=min(x for x in dates if x), affected_nm_ids=[x["internal_nm_id"] for x in before["lines"] if x["line_type"] == "product"], requested_at=now)

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
            current = snapshot(conn, request["shipment_id"])
        checks = [current["protected"] == before["protected"], hashlib.sha256((root / after["file_path"]).read_bytes()).hexdigest() == after["file_sha256"]]
        for section in ("header", "document"):
            checks.append(all(current[section][k] == v for k, v in after[section].items()))
            checks.append(all(current[section][k] == v for k, v in before[section].items() if k not in after[section] and k != "updated_at"))
        by_id = {x["line_id"]: x for x in current["lines"]}
        checks.append(set(by_id) == {x["line_id"] for x in before["lines"]})
        for line in after["lines"]:
            checks.append(all(by_id.get(line["line_id"], {}).get(k) == v for k, v in line["updates"].items()))
            previous = next(x for x in before["lines"] if x["line_id"] == line["line_id"])
            checks.append(all(by_id.get(line["line_id"], {}).get(k) == v for k, v in previous.items() if k not in line["updates"]))
        queue = json.loads(audit["queue_json"])
        return {"operation_id": operation_id, "state": "applied" if all(checks) and queue else "failed", "source_verified": all(checks), "queue": queue, "recovery_reference": str(root / "supplier_invoice_revisions" / operation_id / "before.json")}
