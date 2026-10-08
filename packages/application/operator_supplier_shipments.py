"""Immutable supplier operator acknowledgements; native intents own processing.

The request alias and saved source revision share the native source transaction.
This is an audit/read projection, never a second supplier or financial ledger.
"""
from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import hashlib
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from packages.application import supplier_preparation_intents as intents

DOMAIN = "supplier_shipment"
TABLE = "sheet_vitrina_v1_operator_supplier_receipts"
REJECTIONS = "sheet_vitrina_v1_operator_supplier_rejections"
WIRE_FIELD = "operator_wire_json"
TITLES = {"create": "Создание заказа поставщику", "edit": "Изменение заказа поставщику",
          "archive": "Архивирование заказа поставщику", "rematch": "Сопоставление SKU заказа",
          "price_check": "Проверка цен заказа", "completeness": "Подтверждение полноты расходов"}


def ensure_schema(conn):
    from packages.application.operator_supplier_processing import ensure_schema as ensure_processing
    ensure_processing(conn)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY, request_scope TEXT NOT NULL, request_id TEXT NOT NULL,
        action TEXT NOT NULL, payload_digest TEXT NOT NULL, shipment_id TEXT NOT NULL,
        revision INTEGER NOT NULL, source_digest TEXT NOT NULL, source_json TEXT NOT NULL,
        accepted_at TEXT NOT NULL, actor TEXT NOT NULL, intent_json TEXT NOT NULL,
        wire_digest TEXT NOT NULL DEFAULT '',
        UNIQUE(request_scope,request_id), UNIQUE(shipment_id,revision))""")
    if "wire_digest" not in {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}:
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN wire_digest TEXT NOT NULL DEFAULT ''")
    for event in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS operator_supplier_no_{event.lower()}
            BEFORE {event} ON {TABLE} BEGIN SELECT RAISE(ABORT,'supplier acknowledgement is immutable'); END""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {REJECTIONS}(
        request_scope TEXT NOT NULL, request_id TEXT NOT NULL, action TEXT NOT NULL,
        payload_digest TEXT NOT NULL, shipment_id TEXT NOT NULL, actor TEXT NOT NULL,
        reason TEXT NOT NULL, wire_digest TEXT NOT NULL DEFAULT '', PRIMARY KEY(request_scope,request_id))""")
    if "wire_digest" not in {row[1] for row in conn.execute(f"PRAGMA table_info({REJECTIONS})")}:
        conn.execute(f"ALTER TABLE {REJECTIONS} ADD COLUMN wire_digest TEXT NOT NULL DEFAULT ''")
    for event in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS operator_supplier_rejection_no_{event.lower()}
            BEFORE {event} ON {REJECTIONS} BEGIN SELECT RAISE(ABORT,'supplier rejection is immutable'); END""")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def source_payload(payload):
    """Transport metadata is not a supplier source operand."""
    return {key: value for key, value in payload.items() if key not in {"request_id", WIRE_FIELD}}


def verified_wire_digest(payload):
    """Hash the exact browser text only after binding it to native operands.

    JS and Python number encodings need not agree. The semantic digest remains
    independent and exact; an arbitrary client hash/text cannot certify another
    submitted source. Only the verified digest is retained, never the envelope.
    """
    if WIRE_FIELD not in payload:
        return ""
    text = payload[WIRE_FIELD]
    if not isinstance(text, str):
        raise ValueError("supplier wire identity must be JSON text")
    try:
        operands = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("supplier wire identity must be valid JSON") from exc
    if not isinstance(operands, dict) or digest(operands) != digest(source_payload(payload)):
        raise ValueError("supplier wire identity does not match submitted source operands")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def readonly(db_path):
    conn = sqlite3.connect(Path(db_path).resolve().as_uri()+"?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def capture(conn, shipment_id):
    """Bind all mutable operator source fields, including price-check operands.

    Derived CNY/cost caches are excluded; native preparation captures financial
    documents independently. Stored source paths never enter the public receipt.
    """
    header = conn.execute("SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    if header is None:
        return {"header": {}, "lines": []}
    fields = set(intents.HEADER_FIELDS) | {"created_at", "updated_at", "target_facility_name",
        "contract_no", "contract_date", "supplier_name", "customer_name", "product_qty_total",
        "product_amount_total", "extras_amount_total", "source_filename", "source_file_sha256",
        "source_file_path", "invoice_document_id", "parser_version", "warnings_json", "errors_json",
        "archive_event_id", "archive_reason", "archive_actor", "historical_status_exception"}
    lines = conn.execute("SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines WHERE shipment_id=? ORDER BY line_id", (shipment_id,)).fetchall()
    archive = conn.execute("SELECT event_id,source_fingerprint FROM sheet_vitrina_v1_supplier_shipment_archive_events WHERE event_id=?", (header["archive_event_id"],)).fetchone() if header["archive_event_id"] else None
    return {"header": {key: value for key, value in dict(header).items() if key in fields},
            "lines": [dict(line) for line in lines],
            "preparation_source": intents.capture_source(conn, shipment_id),
            "archive_event": dict(archive) if archive else {}}


def request_context(db_path, *, request_id, request_scope, actor, action, payload, shipment_id="", wire_digest=""):
    identity = str(request_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", identity):
        raise ValueError("supplier request_id must identify one operator action")
    if action not in TITLES:
        raise ValueError("unsupported supplier operator action")
    context = {"request_id": identity, "request_scope": str(request_scope), "actor": str(actor),
               "action": action, "payload_digest": digest(payload), "shipment_id": str(shipment_id), "wire_digest": wire_digest}
    if not Path(db_path).is_file() and action == "create":
        context["expected_source"] = None
        return context, None
    with closing(readonly(db_path)) as conn:
        prior = lookup(conn, context)
        if not prior:
            context["expected_source"] = digest(capture(conn, shipment_id)) if shipment_id else None
    return context, prior


def lookup(conn, context):
    if not _exists(conn, TABLE):
        return None
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_scope=? AND request_id=?",
                       (context["request_scope"], context["request_id"])).fetchone()
    if row and (row["action"] != context["action"] or row["payload_digest"] != context["payload_digest"]
                or (context.get("shipment_id") and row["shipment_id"] != context["shipment_id"])
                or (context.get("wire_digest") and dict(row).get("wire_digest", "") != context["wire_digest"])):
        raise ValueError("supplier request identity already belongs to another action")
    return dict(row) if row else None


def rejection(conn, context):
    row = conn.execute(f"SELECT * FROM {REJECTIONS} WHERE request_scope=? AND request_id=?", (context["request_scope"], context["request_id"])).fetchone() if _exists(conn, REJECTIONS) else None
    if row and (row["action"] != context["action"] or row["payload_digest"] != context["payload_digest"]
                or (context.get("wire_digest") and dict(row).get("wire_digest", "") != context["wire_digest"])):
        raise ValueError("supplier request identity already belongs to another rejected action")
    return row


def record_rejection(runtime, context, error):
    """Retain ONLY a definite native ValueError with no committed source alias.

    Transport/SQLite/process ambiguity remains unknown. An exact committed
    receipt wins over an exception from any later source projection.
    """
    from packages.application.registry_upload_db_backed_runtime import _connect
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_schema(conn)
        if lookup(conn, context):
            conn.rollback()
            return False
        conn.execute(f"INSERT OR IGNORE INTO {REJECTIONS} VALUES(?,?,?,?,?,?,?,?)", (context["request_scope"], context["request_id"], context["action"], context["payload_digest"], context["shipment_id"], context["actor"], str(error).replace("\n", " ")[:500], context.get("wire_digest", "")))
        conn.commit()
        return True


class AlreadySaved(Exception):
    """A concurrent identical request committed before the native writer acquired its lock."""


def before_write(conn, shipment_id, context):
    if not context:
        return
    ensure_schema(conn)
    if context.get("shipment_id") and context["shipment_id"] != shipment_id:
        raise ValueError("supplier acknowledgement target does not match native source")
    if lookup(conn, context):
        raise AlreadySaved()
    if rejection(conn, context):
        raise ValueError("supplier action was rejected; correct the source and use a new request identity")
    current = capture(conn, shipment_id)
    if context["action"] == "create":
        if current["header"]:
            raise ValueError("supplier create source already exists")
    elif context.get("expected_source") != digest(current):
        raise ValueError("supplier source changed before operator save; reload required")
    context["before_intent"] = dict(conn.execute(f"SELECT * FROM {intents.TABLE} WHERE shipment_id=?", (shipment_id,)).fetchone() or {}) if _exists(conn, intents.TABLE) else {}


def record_saved(conn, shipment_id, context):
    if not context:
        return
    if not conn.in_transaction:
        raise ValueError("supplier acknowledgement requires native source transaction")
    source = capture(conn, shipment_id)
    if not source["header"]:
        raise ValueError("supplier acknowledgement has no saved source")
    intent = dict(conn.execute(f"SELECT * FROM {intents.TABLE} WHERE shipment_id=?", (shipment_id,)).fetchone() or {}) if _exists(conn, intents.TABLE) else {}
    # A source-only action is complete at its immutable native save. Existing
    # older pending demand is not relabelled as this action's completed cost.
    bound_intent = intent if intent.get("revision") != context.get("before_intent", {}).get("revision") else {}
    revision = conn.execute(f"SELECT COALESCE(MAX(revision),0)+1 FROM {TABLE} WHERE shipment_id=?", (shipment_id,)).fetchone()[0]
    operation_id = "supplier_" + hashlib.sha256((context["request_scope"]+"|"+context["request_id"]).encode()).hexdigest()[:32]
    conn.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (operation_id,
        context["request_scope"], context["request_id"], context["action"], context["payload_digest"],
        shipment_id, revision, digest(source), _json(source), source["header"]["updated_at"],
        context["actor"], _json(bound_intent), context.get("wire_digest", "")))


def public(conn, row, *, supplier_safe=False):
    source = json.loads(row["source_json"]); bound = json.loads(row["intent_json"])
    state = "accepted"; reason = "native_preparation_pending"; processing = {"kind": "cost_publication", "complete": False}
    if not bound:
        state = "completed"; reason = "source_only_saved"; processing = {"kind": "source_only", "complete": True}
        if row["action"] == "rematch" and any(line.get("line_type") == "product" and not line.get("barcode") for line in source["lines"]):
            reason = "legacy_product_barcode_missing"
            processing["native_state"] = "not_applicable"
    else:
        current = conn.execute(f"SELECT * FROM {intents.TABLE} WHERE shipment_id=?", (row["shipment_id"],)).fetchone() if _exists(conn, intents.TABLE) else None
        if not current or current["revision"] != bound["revision"] or current["source_fingerprint"] != bound["source_fingerprint"]:
            state = "needs_attention"; reason = "native_preparation_error"; processing["native_state"] = "source_changed"
            from packages.application.operator_supplier_processing import operation, superseded
            if superseded(conn, operation(conn, row["operation_id"])):
                reason = "source_superseded"; processing["native_state"] = "superseded"
                processing["terminal"] = True
                replacement = conn.execute(f"SELECT operation_id,source_digest FROM {TABLE} WHERE shipment_id=? AND request_scope=? AND revision>? ORDER BY revision DESC LIMIT 1", (row["shipment_id"], row["request_scope"], row["revision"])).fetchone()
                if replacement and replacement["source_digest"] == digest(capture(conn, row["shipment_id"])):
                    processing["superseded_by"] = {"domain": DOMAIN, "operation_id": replacement["operation_id"],
                        "detail_path": "/sheet-vitrina-v1/supplier?operation_id="+replacement["operation_id"]}
        elif current["status"] == "error":
            state = "needs_attention"; reason = "native_preparation_error"
        elif current["status"] == "delivered":
            state = "processing"; reason = "native_queue_handoff"
        processing.update(preparation_revision=bound["revision"], source_fingerprint=bound["source_fingerprint"],
                          affected_nm_ids=json.loads(bound["affected_nm_ids_json"]), effective_date=bound["effective_date"])
        from packages.application.operator_supplier_processing import public_completion
        completion = public_completion(conn, row["operation_id"])
        if completion:
            if completion["complete"] or not processing.get("terminal"):
                state = completion["state"]
            processing.update(complete=completion["complete"], completion_reason=completion["reason_code"])
            if completion["complete"]:
                reason = "cost_not_changed" if completion.get("effect") == "derived_no_change" else "exact_cost_published"
                if completion.get("effect") == "derived_no_change":processing["kind"] = "derived_no_change"
                processing.update(completed_at=completion["completed_at"], calculation=completion["calculation"])
    header = source["header"]
    if supplier_safe:
        processing = {key: value for key, value in processing.items() if key in {"kind", "complete", "native_state", "terminal", "superseded_by", "completed_at", "calculation"}}
    fields = [{"label": "Заказ", "value": header.get("invoice_no") or row["shipment_id"]},
              {"label": "Действие", "value": TITLES[row["action"]]}, {"label": "Версия", "value": str(row["revision"])}]
    return {"contract_name": "operator_operations_v1", "domain": DOMAIN, "durable_saved": True,
        "operation_id": row["operation_id"], "accepted_at": row["accepted_at"], "actor": row["actor"],
        "state": state, "reason_code": reason, "title_ru": TITLES[row["action"]], "fields": fields,
        "reason_ru": {"source_only_saved": "Изменение источника сохранено. Расчёт себестоимости не требуется.",
            "cost_not_changed": "Документ сохранён. Складская себестоимость не изменилась.",
            "exact_cost_published": "Расчёт этой версии опубликован. Полнота расходов подтверждается отдельно.",
            "legacy_product_barcode_missing": "Сопоставление пропущено: в старом invoice нет штрихкодов. Требуется повторный разбор invoice.",
            "source_superseded": "Эту версию заменили последующими изменениями.",
            "native_queue_handoff": "Документ сохранён. Ожидает обработки.",
            "native_preparation_error": "Подготовка документа требует внимания.",
            "native_preparation_pending": "Документ сохранён. Ожидает обработки."}[reason],
        "primary_effect": "source_saved", "processing": processing,
        "source_ref": {"domain": DOMAIN, "entity_id": row["shipment_id"], "revision": row["revision"],
                       "digest": row["source_digest"], "action": row["action"]},
        "journal_path": "/sheet-vitrina-v1/operations?operation_id="+row["operation_id"],
        "detail_path": "/sheet-vitrina-v1/supplier?operation_id="+row["operation_id"]}


def read_request(db_path, request_id, *, request_scope, supplier_safe=False):
    if not Path(db_path).is_file():
        return {"domain": DOMAIN, "request_id": request_id, "status": "unknown", "acceptance": None}
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_scope=? AND request_id=?", (request_scope, request_id)).fetchone() if _exists(conn, TABLE) else None
        if row is None and _exists(conn, REJECTIONS):
            rejected = conn.execute(f"SELECT * FROM {REJECTIONS} WHERE request_scope=? AND request_id=?", (request_scope, request_id)).fetchone()
            if rejected:
                return {"domain": DOMAIN, "request_id": request_id, "status": "rejected", "action": rejected["action"],
                    "payload_digest": rejected["payload_digest"], "wire_digest": dict(rejected).get("wire_digest", ""), "acceptance": None, "shipment": None,
                    "error": "Заказ не принят. Проверьте введённые данные." if supplier_safe else rejected["reason"]}
        return {"domain": DOMAIN, "request_id": request_id, "status": "accepted" if row else "unknown",
                "action": row["action"] if row else None, "payload_digest": row["payload_digest"] if row else None,
                "wire_digest": dict(row).get("wire_digest", "") if row else None,
                "acceptance": public(conn, row, supplier_safe=supplier_safe) if row else None,
                "shipment": saved_projection(row, supplier_safe=supplier_safe) if row else None}


def read_acceptance(db_path, operation_id, *, request_scope, supplier_safe=False):
    # Principal scope is mandatory; a global journal must additionally enforce
    # its native supply/supplier grants BEFORE count/search/detail and use this
    # safe projection, never expose source_json or persisted file paths.
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=? AND request_scope=?", (operation_id, request_scope)).fetchone() if _exists(conn, TABLE) else None
        return public(conn, row, supplier_safe=supplier_safe) if row else None


def read_operation(db_path, operation_id, *, request_scope, supplier_safe=False):
    """Exact source detail for its native form host; permission precedes this."""
    unknown = {"domain": DOMAIN, "operation_id": operation_id, "status": "unknown", "acceptance": None}
    if not Path(db_path).is_file():
        return unknown
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f"SELECT request_id FROM {TABLE} WHERE operation_id=? AND request_scope=?", (operation_id, request_scope)).fetchone() if _exists(conn, TABLE) else None
    if not row:
        return unknown
    result = read_request(db_path, row["request_id"], request_scope=request_scope, supplier_safe=supplier_safe)
    return result if result["acceptance"] and result["acceptance"]["operation_id"] == operation_id else unknown


def saved_projection(row, *, supplier_safe=False):
    from packages.application.supplier_shipments import _detail_payload, _supplier_safe_detail_projection
    from packages.application.supplier_shipment_status import supplier_business_today, apply_derived_supplier_status
    source = json.loads(row["source_json"])
    header = source["header"]
    header["warnings"] = json.loads(header.get("warnings_json") or "[]")
    header["errors"] = json.loads(header.get("errors_json") or "[]")
    header.pop("source_file_path", None)
    source["lines"].sort(key=lambda line: (line.get("sort_order") or 0, line["line_id"]))
    for line in source["lines"]:
        line["raw"] = json.loads(line.get("raw_json") or "{}")
        line["price_conformity_context"] = json.loads(line.get("price_conformity_context_json") or "{}")
        line["manual_override"] = bool(line.get("manual_override"))
        for key in ("source_product_type", "source_match_key", "nomenclature_item_id", "match_evidence", "model_diagnostic"):
            line[key] = line["raw"].get(key, {} if key in {"match_evidence", "model_diagnostic"} else "")
    archived = bool(header.get("archived_at"))
    # Native active-card normalizer intentionally rejects the archive status;
    # an immutable archive action remains readable in this audit projection.
    payload = _detail_payload({**source, "header": {**header, "order_status": "production"} if archived else header})
    if archived:
        payload.update(order_status="archived", persisted_order_status="archived")
    else:
        payload = apply_derived_supplier_status(payload, business_today=supplier_business_today(timestamp=row["accepted_at"]))
    # This is the source's declared approximation, never a paid balance or a
    # derived cost publication. Preserve the native card's simple source fields.
    rate = header.get("approx_yuan_rate")
    total = header.get("invoice_amount_total")
    payload["approx_invoice_cost_rub"] = float(Decimal(str(rate))*Decimal(str(total))) if rate and total else None
    payload["approx_landed_cost_per_unit_rub"] = None
    if not supplier_safe:
        from packages.application.supplier_financial_documents import build_financial_summary
        operands = source.get("preparation_source", {})
        documents = [{**doc, "normalized_parse": doc.get("normalized_parse_json") or {}} for doc in operands.get("documents", [])]
        expenses = [{**line, "raw": line.get("raw_json") or {}} for line in operands.get("expenses", [])]
        summary = build_financial_summary(documents, expenses, shipment=source)
        payload["approx_landed_cost_per_unit_rub"] = summary.get("per_unit", {}).get("approx_landed_cost_per_unit_rub")
    if supplier_safe:
        payload = _supplier_safe_detail_projection(payload, business_today=supplier_business_today(timestamp=row["accepted_at"]))
    payload.update(request_id=row["request_id"], operation_applied=True, durable_saved=True)
    payload.setdefault("contract_name", "sheet_vitrina_v1_supplier_shipments")
    payload["status"] = "ok"
    if row["action"] == "archive":
        payload.update(deleted=True, archived=True, source_fingerprint=source.get("archive_event", {}).get("source_fingerprint", ""))
    return payload


def saved_result(db_path, context, *, supplier_safe=False):
    """Reopen the immutable version, including an archive, without native GET side effects."""
    with closing(readonly(db_path)) as conn:
        row = lookup(conn, context)
        if not row:
            raise ValueError("supplier action has no committed acknowledgement")
        payload = saved_projection(row, supplier_safe=supplier_safe)
        payload["acceptance"] = public(conn, row, supplier_safe=supplier_safe)
        return payload


def execute(runtime, *, action, payload, shipment_id="", actor="operator", request_scope="local_operator", supplier_safe=False, write):
    wire_digest = verified_wire_digest(payload)
    identity = str(payload.get("request_id") or "").strip()
    if not identity:
        # Compatibility callers receive a native receipt in the response. New
        # browser adapters always retain their client identity BEFORE submission.
        identity = "supplier_"+uuid4().hex
    # Serialize just this principal/request before native invoice/file staging.
    # The native writer still independently checks alias and source CAS. No
    # worker, queue or continuation is owned by this bounded request lock.
    lock_dir = Path(runtime.runtime_dir)/"operator_supplier_request_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256((str(request_scope)+"|"+identity).encode()).hexdigest()+".lock"
    with (lock_dir/lock_name).open("a") as handle:
        deadline = time.monotonic()+5
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("supplier action is still saving; read the same request identity")
                time.sleep(0.01)
        try:
            context, prior = request_context(runtime.db_path, request_id=identity, request_scope=request_scope,
                actor=actor, action=action, payload=source_payload(payload), shipment_id=shipment_id, wire_digest=wire_digest)
            if not prior:
                if Path(runtime.db_path).is_file():
                    with closing(readonly(runtime.db_path)) as conn:
                        rejected = rejection(conn, context)
                        if rejected:
                            raise ValueError("supplier action was rejected; use a new request identity after correction")
                try:
                    write(context)
                except AlreadySaved:
                    pass
                except ValueError as exc:
                    if record_rejection(runtime, context, exc):
                        raise
            return saved_result(runtime.db_path, context, supplier_safe=supplier_safe)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
