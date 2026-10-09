"""Exact durable confirmation aliases for the native factual-date worker.

The ephemeral preview is authority only before admission. Native job and its
immutable request binding commit together in the operational database. GET is
query-only and never starts a worker or repeats a source operation.
"""
from contextlib import closing
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time

from packages.application import operator_supplier_shipments as source

DOMAIN = "supplier_factual_date"
TABLE = "sheet_vitrina_v1_operator_supplier_factual_requests"
APPLIED = "sheet_vitrina_v1_operator_supplier_factual_applied"
JOB = "sheet_vitrina_v1_supplier_shipment_factual_corrections"


def ensure_schema(conn):
    from packages.application.operator_supplier_processing import ensure_schema as ensure_processing
    ensure_processing(conn)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        request_scope TEXT NOT NULL,request_id TEXT NOT NULL,shipment_id TEXT NOT NULL,
        confirmation_token TEXT NOT NULL,payload_digest TEXT NOT NULL,wire_digest TEXT NOT NULL,
        actor TEXT NOT NULL,accepted_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,
        correction_id TEXT NOT NULL,request_fingerprint TEXT NOT NULL,
        source_digest TEXT NOT NULL,source_json TEXT NOT NULL,
        PRIMARY KEY(request_scope,request_id))""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {APPLIED}(
        correction_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,plan_fingerprint TEXT NOT NULL,
        proof_digest TEXT NOT NULL,proof_json TEXT NOT NULL)""")
    for table in (TABLE, APPLIED):
        for event in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{event.lower()}_immutable
                BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'factual acknowledgement is immutable'); END""")


def operands(conn, shipment_id):
    return {"supplier": source.capture(conn, shipment_id),
        "documents": [dict(r) for r in conn.execute("SELECT document_id,updated_at,parse_status,file_sha256 FROM sheet_vitrina_v1_supplier_financial_documents WHERE supplier_order_id=? ORDER BY document_id", (shipment_id,))],
        "cny": [dict(r) for r in conn.execute("SELECT document_id,updated_at,status,source_order_id,file_sha256 FROM sheet_vitrina_v1_cny_documents WHERE source_order_id=? ORDER BY document_id", (shipment_id,))]}


def _lookup(conn, context):
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_scope=? AND request_id=?", (context["request_scope"], context["request_id"])).fetchone() if source._exists(conn, TABLE) else None
    if row and any(row[k] != context[k] for k in ("shipment_id", "payload_digest", "wire_digest")):
        raise ValueError("factual request identity belongs to another confirmation")
    return dict(row) if row else None


def before_accept(conn, context):
    ensure_schema(conn)
    if _lookup(conn, context):
        raise source.AlreadySaved()
    if source.digest(operands(conn, context["shipment_id"])) != context["source_digest"]:
        raise ValueError("supplier factual source changed before native request acceptance")
    prior = conn.execute(f"SELECT request_scope FROM {TABLE} WHERE confirmation_token=? AND status='accepted' LIMIT 1", (context["confirmation_token"],)).fetchone()
    if prior and prior[0] != context["request_scope"]:
        raise ValueError("confirmation token belongs to another operator")


def record_accepted(conn, context, job):
    if not conn.in_transaction:
        raise ValueError("factual receipt requires the native job transaction")
    if job["actor"] != context["actor"] or job["shipment_id"] != context["shipment_id"]:
        raise ValueError("native factual job belongs to another operator or shipment")
    prior = conn.execute(f"SELECT source_digest FROM {TABLE} WHERE correction_id=? AND status='accepted' LIMIT 1", (job["correction_id"],)).fetchone()
    if prior and prior[0] != context["source_digest"]:
        raise ValueError("existing factual job belongs to another source revision")
    _insert(conn, {**context, "status": "accepted", "reason": "", "correction_id": job["correction_id"],
        "request_fingerprint": job["request_fingerprint"], "accepted_at": job["requested_at"]})


def _insert(conn, row):
    keys = ("request_scope", "request_id", "shipment_id", "confirmation_token", "payload_digest", "wire_digest",
            "actor", "accepted_at", "status", "reason", "correction_id", "request_fingerprint", "source_digest", "source_json")
    conn.execute(f"INSERT INTO {TABLE} VALUES({','.join('?' for _ in keys)})", tuple(row[k] for k in keys))


def before_apply(conn, correction_id):
    if not source._exists(conn, TABLE):
        return  # Legacy native worker has no new operator acknowledgement.
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE correction_id=? AND status='accepted' LIMIT 1", (correction_id,)).fetchone()
    if row and source.digest(operands(conn, row["shipment_id"])) != row["source_digest"]:
        raise ValueError("accepted factual source revision changed before atomic apply")
    return source.intents.begin_source_change(conn, [row["shipment_id"]]) if row else None


def record_applied(conn, correction_id, *, plan, version_id, publication_id, queue_id):
    if not source._exists(conn, TABLE):
        return
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE correction_id=? AND status='accepted' LIMIT 1", (correction_id,)).fetchone()
    if not row:
        return
    proof = {"correction_id": correction_id, "request_fingerprint": row["request_fingerprint"],
        "source_before_digest": row["source_digest"], "source_after": operands(conn, row["shipment_id"]),
        "version_id": version_id, "plan_fingerprint": plan["plan_fingerprint"], "publication_id": publication_id,
        "queue_id": queue_id, "affected_nm_ids": plan["affected_nm_ids"], "effective_date": plan["earliest_business_date"]}
    proof["preparation_intent"] = dict(conn.execute(f"SELECT * FROM {source.intents.TABLE} WHERE shipment_id=?", (row["shipment_id"],)).fetchone() or {})
    conn.execute(f"INSERT INTO {APPLIED} VALUES(?,?,?,?,?)", (correction_id, version_id, plan["plan_fingerprint"], source.digest(proof), source._json(proof)))


def applied_proof(conn, row):
    from packages.application.warehouse_targeted_replay import TARGETED_PUBLICATION_TABLE
    saved = conn.execute(f"SELECT * FROM {APPLIED} WHERE correction_id=?", (row["correction_id"],)).fetchone() if source._exists(conn, APPLIED) else None
    if not saved:
        return None
    proof = json.loads(saved["proof_json"])
    publication = conn.execute(f"SELECT * FROM {TARGETED_PUBLICATION_TABLE} WHERE publication_id=?", (proof["publication_id"],)).fetchone()
    if (source.digest(proof) != saved["proof_digest"] or proof["correction_id"] != row["correction_id"]
            or proof["version_id"] != saved["version_id"] or proof["plan_fingerprint"] != saved["plan_fingerprint"]
            or proof["request_fingerprint"] != row["request_fingerprint"] or proof["source_before_digest"] != row["source_digest"]
            or not publication or publication["status"] != "complete" or publication["version_id"] != saved["version_id"]
            or publication["plan_fingerprint"] != saved["plan_fingerprint"]):
        raise ValueError("factual native applied proof is no longer valid")
    return proof


def recover_applied_status(runtime, block, correction_id):
    """Cheap native publication revalidation and job-state CAS share a writer."""
    from packages.application.registry_upload_db_backed_runtime import _connect
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE correction_id=? AND status='accepted' LIMIT 1", (correction_id,)).fetchone()
        retained = applied_proof(conn, row)
        if not retained:
            raise ValueError("factual applied proof disappeared before recovery")
        now = block.timestamp_factory()
        conn.execute(f"""UPDATE {JOB} SET status='success',phase='completed',
            progress_text='Изменение восстановлено по сохранённому доказательству',updated_at=?,completed_at=?,
            report_json=?,error_code='',error_message='' WHERE correction_id=? AND request_fingerprint=?
            AND status IN ('queued','running')""", (now, now, source._json({"status":"ready","applied":True,"native_applied_proof":retained}), correction_id, row["request_fingerprint"]))
        conn.commit()


def consume(runtime, *, block=None):
    """Existing warehouse worker drains only admitted native factual jobs."""
    from packages.application.business_data_heavy_admission import HeavyAdmissionBusy, heavy_admitted
    from packages.application.business_data_procedure_admission import MaintenanceAdmissionBlocked
    from packages.application.supplier_shipment_factual_correction import SupplierShipmentFactualCorrectionBlock
    block = block or SupplierShipmentFactualCorrectionBlock(runtime=runtime)
    if not Path(runtime.db_path).is_file():
        return {"status": "no_op", "requests": []}
    try:
        with heavy_admitted(runtime.runtime_dir, operation="supplier-factual"):
            with closing(source.readonly(runtime.db_path)) as conn:
                if not source._exists(conn, TABLE):
                    return {"status": "no_op", "requests": []}
                pending = [dict(r) for r in conn.execute(f"SELECT DISTINCT j.* FROM {JOB} j JOIN {TABLE} r ON r.correction_id=j.correction_id WHERE j.status IN ('queued','running') ORDER BY j.requested_at,j.correction_id")]
            results = []
            for job in pending:
                try:
                    with closing(source.readonly(runtime.db_path)) as conn:
                        row = conn.execute(f"SELECT * FROM {TABLE} WHERE correction_id=? AND status='accepted' LIMIT 1", (job["correction_id"],)).fetchone()
                        retained = applied_proof(conn, row)
                        if not retained and source.digest(operands(conn, row["shipment_id"])) != row["source_digest"]:
                            raise ValueError("accepted factual source revision changed before worker")
                    if retained:
                        # End the pinned reader before the native status write.
                        recover_applied_status(runtime, block, job["correction_id"])
                        results.append({"correction_id": job["correction_id"], "status": "applied"});continue
                    result = block.run_job(job["correction_id"])
                    results.append({"correction_id": job["correction_id"], "status": result["status"]})
                except Exception as exc:
                    block._set_job_state(job["correction_id"], status="needs_review",
                        phase="requires_review", progress_text="Требует проверки сохранённой версии", completed=True,
                        error_code=type(exc).__name__, error_message=str(exc)[:500])
                    results.append({"correction_id": job["correction_id"], "status": "needs_review"})
            return {"status": "processed" if results else "no_op", "requests": results}
    except (HeavyAdmissionBusy, MaintenanceAdmissionBlocked):
        return {"status": "pending", "requests": []}


def accept(entry, shipment_id, payload, *, actor, request_scope):
    from packages.application.registry_upload_db_backed_runtime import _connect
    identity = str(payload.get("request_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", identity):
        raise ValueError("factual request_id must identify one confirmation")
    context = {"request_scope": request_scope, "request_id": identity, "shipment_id": shipment_id,
        "actor": actor, "payload_digest": source.digest(source.source_payload(payload)),
        "wire_digest": source.verified_wire_digest(payload), "confirmation_token": str(payload.get("confirmation_token") or "")}
    lock = Path(entry.runtime.runtime_dir)/"operator_supplier_request_locks"
    lock.mkdir(parents=True, exist_ok=True)
    with (lock/(hashlib.sha256((request_scope+"|"+identity).encode()).hexdigest()+".lock")).open("a") as handle:
        deadline = time.monotonic()+5
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("factual request is saving; read the same identity")
                time.sleep(.01)
        try:
            with closing(source.readonly(entry.runtime.db_path)) as conn:
                prior = _lookup(conn, context)
                if prior:
                    return read(entry.runtime.db_path, identity, shipment_id=shipment_id, request_scope=request_scope)
                captured = operands(conn, shipment_id)
            context.update(source_digest=source.digest(captured), source_json=source._json(captured),
                accepted_at=entry.activated_at_factory(), status="rejected", reason="", correction_id="", request_fingerprint="")
            try:
                preview = entry.supplier_shipments_block.validate_factual_dates_confirmation(shipment_id, context["confirmation_token"])
                if preview.get("consumed_at"):
                    raise ValueError("confirmation already consumed; read its original request identity")
                changes = (preview.get("payload") or {}).get("changes") or []
                if len(changes) != 1 or changes[0].get("field") != "actual_shipment_date":
                    raise ValueError("factual shipment date must be confirmed separately")
                entry.supplier_shipment_factual_correction_block.create_job(shipment_id=shipment_id,
                    new_actual_shipment_date=changes[0]["new_value"], actor=actor, operator_request=context)
            except source.AlreadySaved:
                pass
            except ValueError as exc:
                with _connect(entry.runtime.db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE");ensure_schema(conn)
                    if not _lookup(conn, context):
                        _insert(conn, {**context, "reason": str(exc)[:500]})
                    conn.commit()
                raise
            # Auxiliary preview consumption is recoverable: immutable main-DB
            # job+alias already owns acceptance. It cannot turn a save into refusal.
            result = read(entry.runtime.db_path, identity, shipment_id=shipment_id, request_scope=request_scope)
            try:
                entry.runtime.complete_supplier_confirmation_preview(token=context["confirmation_token"],
                    consumed_at=context["accepted_at"], result=result)
            except Exception:
                pass
            return result
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read(db_path, request_id, *, shipment_id, request_scope):
    unknown = {"domain": DOMAIN, "request_id": request_id, "status": "unknown", "acceptance": None}
    if not Path(db_path).is_file():
        return unknown
    with closing(source.readonly(db_path)) as conn:
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_scope=? AND request_id=? AND shipment_id=?", (request_scope, request_id, shipment_id)).fetchone() if source._exists(conn, TABLE) else None
        if not row:
            return unknown
        row = dict(row)
        result = {"domain": DOMAIN, "request_id": request_id, "action": "factual_date", "payload_digest": row["payload_digest"], "wire_digest": row["wire_digest"], "status": row["status"], "acceptance": None, "shipment": None}
        if row["status"] == "rejected":
            return {**result, "error": row["reason"]}
        job = conn.execute(f"SELECT * FROM {JOB} WHERE correction_id=?", (row["correction_id"],)).fetchone()
        if not job or job["request_fingerprint"] != row["request_fingerprint"] or job["shipment_id"] != shipment_id or job["actor"] != row["actor"]:
            return unknown
        proof_problem = None
        try:
            applied = applied_proof(conn, row)
        except ValueError as exc:
            applied = None; proof_problem = str(exc)
        state = "needs_attention" if proof_problem or job["status"] in {"error", "needs_review"} else "processing"
        snapshot = json.loads(row["source_json"])["supplier"]
        if applied:
            snapshot = applied["source_after"]["supplier"]
        receipt = {"domain": DOMAIN, "durable_saved": True, "operation_id": row["correction_id"], "accepted_at": row["accepted_at"],
            "state": state, "title_ru": "Изменение фактической даты", "actor": row["actor"], "primary_effect": "request_saved",
            "physical_applied": None if proof_problem else bool(applied), "processing": {"kind": "factual_correction", "complete": False, "native_state": job["status"]},
            "fields": [{"label": "Заказ", "value": snapshot["header"].get("invoice_no") or shipment_id}, {"label": "Фактическая дата", "value": job["new_value"]}],
            "source_ref": {"domain": DOMAIN, "entity_id": shipment_id, "action": "factual_date", "digest": row["source_digest"], "native_id": row["correction_id"]},
            "reason_ru": "Запрос сохранён. Фактическое применение и публикация расчёта проверяются отдельно.",
            "journal_path": "/sheet-vitrina-v1/operations?operation_id="+row["correction_id"],
            "detail_path": "/sheet-vitrina-v1/supplier?operation_id="+row["correction_id"]}
        expected = applied["source_after"] if applied else json.loads(row["source_json"])
        if source.digest(operands(conn, shipment_id)) != source.digest(expected):
            receipt["state"] = "needs_attention"
            receipt["reason_ru"] = "Эту версию заменили последующими изменениями."
            receipt["processing"].update(terminal=True, native_state="superseded")
            replacement = conn.execute(f"SELECT operation_id,source_digest FROM {source.TABLE} WHERE shipment_id=? AND request_scope=? ORDER BY revision DESC LIMIT 1", (shipment_id, request_scope)).fetchone() if source._exists(conn, source.TABLE) else None
            if replacement and replacement["source_digest"] == source.digest(source.capture(conn, shipment_id)):
                receipt["processing"]["superseded_by"] = {"domain": source.DOMAIN, "operation_id": replacement["operation_id"], "detail_path": "/sheet-vitrina-v1/supplier?operation_id="+replacement["operation_id"]}
        if applied:
            from packages.application.operator_supplier_processing import public_completion
            completion = public_completion(conn, row["correction_id"])
            if completion:
                if completion["complete"] or not receipt["processing"].get("terminal"):
                    receipt["state"] = completion["state"]
                receipt["processing"].update(complete=completion["complete"], completion_reason=completion["reason_code"])
                if completion["complete"]:
                    receipt["processing"]["completed_at"] = completion["completed_at"]
                    receipt["processing"]["calculation"] = completion["calculation"]
                    receipt["reason_ru"] = "Дата применена. Расчёт этой версии опубликован. Полнота расходов подтверждается отдельно."
        projected = source.saved_projection({"source_json": source._json(snapshot), "request_id": request_id, "accepted_at": row["accepted_at"], "action": "edit"})
        projected.update(operation_applied=bool(applied), acceptance=receipt, request_saved=True)
        return {**result, "acceptance": receipt, "shipment": projected}


def read_operation(db_path, operation_id, *, request_scope):
    unknown = {"domain": DOMAIN, "operation_id": operation_id, "status": "unknown", "acceptance": None}
    if not Path(db_path).is_file():
        return unknown
    with closing(source.readonly(db_path)) as conn:
        row = conn.execute(f"SELECT request_id,shipment_id FROM {TABLE} WHERE correction_id=? AND request_scope=? AND status='accepted' ORDER BY accepted_at,request_id LIMIT 1", (operation_id, request_scope)).fetchone() if source._exists(conn, TABLE) else None
    if not row:
        return unknown
    result = read(db_path, row["request_id"], shipment_id=row["shipment_id"], request_scope=request_scope)
    return result if result["acceptance"] and result["acceptance"]["operation_id"] == operation_id else unknown
