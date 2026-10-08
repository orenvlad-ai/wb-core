"""Immutable fulfillment source receipts, not another queue or cost ledger.

Public readers are snapshot-pinned and strictly read-only. Completion is a private
native-owner seam; it requires independent downstream readback, not queue flags.
"""
from __future__ import annotations

from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from packages.application import fulfillment_recalc_intents as intents

DOMAIN = "fulfillment_services"
TABLE = "sheet_vitrina_v1_operator_fulfillment_receipts"
COMPLETIONS = "sheet_vitrina_v1_operator_fulfillment_completions"
ATTEMPTS = "sheet_vitrina_v1_operator_fulfillment_attempts"
REQUESTS = "sheet_vitrina_v1_operator_fulfillment_requests"
FUNCTIONAL_PROOFS = "sheet_vitrina_v1_operator_fulfillment_functional_proofs"
HEADER = ("upload_id", "original_filename", "file_sha256", "uploaded_at", "validation_status",
          "rows_total", "rows_matched", "amount_without_vat_total", "vat_total",
          "amount_with_vat_total", "payment_validation_id")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    # execute, not executescript: never implicitly commit the source transaction.
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY, upload_id TEXT NOT NULL, action TEXT NOT NULL,
        source_revision TEXT NOT NULL, source_json TEXT NOT NULL, source_digest TEXT NOT NULL,
        accepted_at TEXT NOT NULL, actor TEXT NOT NULL,
        UNIQUE(upload_id,action,source_revision))""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {COMPLETIONS}(
        operation_id TEXT PRIMARY KEY REFERENCES {TABLE}(operation_id),
        proof_json TEXT NOT NULL, proof_digest TEXT NOT NULL, completed_at TEXT NOT NULL)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {FUNCTIONAL_PROOFS}(
        operation_id TEXT NOT NULL REFERENCES {TABLE}(operation_id),
        warehouse_version_id TEXT NOT NULL, proof_json TEXT NOT NULL, proof_digest TEXT NOT NULL,
        PRIMARY KEY(operation_id,warehouse_version_id))""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {ATTEMPTS}(
        operation_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
        state TEXT NOT NULL, reason_code TEXT NOT NULL)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {REQUESTS}(
        request_scope TEXT NOT NULL, request_id TEXT NOT NULL, action TEXT NOT NULL,
        payload_digest TEXT NOT NULL, upload_id TEXT NOT NULL, operation_id TEXT,
        PRIMARY KEY(request_scope,request_id))""")
    for table in (TABLE, COMPLETIONS, FUNCTIONAL_PROOFS, REQUESTS):
        for action in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'Fulfillment receipts are immutable'); END""")


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _source(conn: sqlite3.Connection, upload_id: str) -> dict[str, Any]:
    upload = conn.execute(f"SELECT * FROM {intents.UPLOADS} WHERE upload_id=?", (upload_id,)).fetchone()
    if upload is None:
        raise ValueError("fulfillment_source_missing")
    lines = [{key: value for key, value in dict(row).items() if key not in {"id", "created_at"}}
             for row in conn.execute(f"SELECT * FROM {intents.LINES} WHERE upload_id=? ORDER BY row_index,id", (upload_id,))]
    return {"header": {key: upload[key] for key in HEADER}, "lines_digest": digest(lines)}


def record_source(conn: sqlite3.Connection, upload_id: str, *, action: str, actor: str = "") -> str | None:
    """Called only for a new explicit final action, in source+intent transaction."""
    if not conn.in_transaction:
        raise ValueError("fulfillment_receipt_requires_source_transaction")
    if action not in {"upload", "delete"}:
        raise ValueError("invalid_fulfillment_action")
    upload = conn.execute(f"SELECT * FROM {intents.UPLOADS} WHERE upload_id=?", (upload_id,)).fetchone()
    if upload is None or upload["validation_status"] != "ok":
        return None  # Failed parsing/validation is diagnostic, not final acceptance.
    if bool(upload["deleted_at"]) != (action == "delete"):
        raise ValueError("fulfillment_action_source_mismatch")
    revision = intents.source_revision(upload)
    operation_id = "ffsvc_" + digest([upload_id, action, revision]).removeprefix("sha256:")[:28]
    source = {**_source(conn, upload_id), "action": action, "source_revision": revision}
    if action == "delete":
        source["deletion"] = {key: upload[key] for key in ("deleted_at", "deleted_by", "delete_reason")}
    accepted_at = upload["deleted_at"] if action == "delete" else upload["uploaded_at"]
    conn.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES(?,?,?,?,?,?,?,?)", (
        operation_id, upload_id, action, revision, canonical(source), digest(source), accepted_at,
        str(upload["deleted_by"] or "") if action == "delete" else actor))
    existing = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (operation_id,)).fetchone()
    if existing["source_digest"] != digest(source):
        raise ValueError("fulfillment_receipt_source_conflict")
    return operation_id


def request_identity(request_id: str, request_scope: str = "") -> tuple[str, str]:
    import re
    identity = str(request_id or "").strip()
    if identity and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", identity):
        raise ValueError("invalid_fulfillment_request_id")
    return identity, str(request_scope or "")[:160]


def request_ref(conn, *, request_id, request_scope="", action, payload_digest):
    identity, scope = request_identity(request_id, request_scope)
    if not identity or not _exists(conn, REQUESTS):
        return None
    row = conn.execute(f"SELECT * FROM {REQUESTS} WHERE request_scope=? AND request_id=?", (scope, identity)).fetchone()
    if row and (row["action"] != action or row["payload_digest"] != payload_digest):
        raise ValueError("fulfillment_request_identity_conflict")
    return row


def bind_request(conn, *, request_id, request_scope="", action, payload_digest, upload_id):
    identity, scope = request_identity(request_id, request_scope)
    if not identity:
        return
    if not conn.in_transaction:
        raise ValueError("fulfillment_request_requires_source_transaction")
    prior = request_ref(conn, request_id=identity, request_scope=scope, action=action, payload_digest=payload_digest)
    if prior:
        if prior["upload_id"] != upload_id:
            raise ValueError("fulfillment_request_identity_conflict")
        return
    source = conn.execute(f"SELECT * FROM {intents.UPLOADS} WHERE upload_id=?", (upload_id,)).fetchone()
    operation = conn.execute(f"SELECT operation_id FROM {TABLE} WHERE upload_id=? AND action=? AND source_revision=?",
                            (upload_id, action, intents.source_revision(source))).fetchone()
    conn.execute(f"INSERT INTO {REQUESTS} VALUES(?,?,?,?,?,?)", (scope, identity, action, payload_digest,
                 upload_id, operation[0] if operation else None))


def read_request(db_path, request_id, *, request_scope=""):
    identity, scope = request_identity(request_id, request_scope)
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f"SELECT * FROM {REQUESTS} WHERE request_scope=? AND request_id=?", (scope, identity)).fetchone() if _exists(conn, REQUESTS) else None
        if row is None:
            return {"domain": DOMAIN, "request_id": identity, "status": "not_found", "acceptance": None}
        operation = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (row["operation_id"],)).fetchone() if row["operation_id"] else None
        return {"domain": DOMAIN, "request_id": identity, "action": row["action"],
                "payload_digest": row["payload_digest"], "upload_id": row["upload_id"],
                "status": "accepted" if operation else ("not_tracked" if conn.execute(
                    f"SELECT 1 FROM {intents.UPLOADS} WHERE upload_id=? AND validation_status='ok'", (row["upload_id"],)).fetchone() else "rejected"),
                "acceptance": _public(conn, operation) if operation else None}


def readonly(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def _ref(row: Any, source: dict[str, Any]) -> dict[str, Any]:
    return {"domain": DOMAIN, "entity_id": row["upload_id"], "action": row["action"],
            "source_revision": row["source_revision"], "source_sha256": source["header"]["file_sha256"]}


def _current(conn: sqlite3.Connection, row: Any, source: dict[str, Any]) -> str:
    try:
        current_source = _source(conn, row["upload_id"])
    except ValueError:
        return "source_changed"
    if digest(source) != row["source_digest"] or current_source != {
            key: source[key] for key in ("header", "lines_digest")}:
        return "source_changed"
    upload = conn.execute(f"SELECT * FROM {intents.UPLOADS} WHERE upload_id=?", (row["upload_id"],)).fetchone()
    if intents.source_revision(upload) != row["source_revision"]:
        return "superseded"
    if row["action"] == "delete" and source["deletion"] != {
            key: upload[key] for key in ("deleted_at", "deleted_by", "delete_reason")}:
        return "source_changed"
    return "current"


def _request(conn: sqlite3.Connection, row: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not _exists(conn, intents.TABLE):
        return None, None
    request = conn.execute(f"SELECT * FROM {intents.TABLE} WHERE upload_id=? AND source_revision=?",
                           (row["upload_id"], row["source_revision"])).fetchone()
    if request is None:
        return None, None
    queue = None
    if request["queue_id"] and _exists(conn, intents.QUEUE):
        queue = conn.execute(f"SELECT * FROM {intents.QUEUE} WHERE queue_id=? AND stable_source_id=? AND source_revision=?",
                             (request["queue_id"], "fulfillment_upload:" + row["upload_id"], row["source_revision"])).fetchone()
    return dict(request), dict(queue) if queue else None


def _queue_ref(queue: dict[str, Any]) -> dict[str, Any]:
    return {key: queue[key] for key in ("queue_id", "stable_source_id", "source_revision", "effective_date")} | {
        "affected_nm_ids": sorted(json.loads(queue["affected_nm_ids_json"]))}


def _overlay_evidence(conn: sqlite3.Connection, request: dict[str, Any]) -> dict[str, Any]:
    """Bind full live native supply authority to its persisted cost operands.

    The denominator includes every packed goods row, including SKUs outside the
    active catalogue. Native quantity accessors and supply revision are shared
    with the materializer and WarehouseFunctional; no UI/filter quantity is used.
    """
    from decimal import Decimal, InvalidOperation
    from math import isclose
    from packages.application.fulfillment_services import FulfillmentServicesBlock
    from packages.application.our_wb_costs import (
        _parse_wb_goods, _wb_supply_row_to_dict, _wb_good_packed_quantity,
        _wb_good_accepted_quantity, _wb_good_quantity_is_final_accepted,
        _selected_wb_supply_inputs, _stable_hash,
    )
    from packages.application.warehouse_functional import _supply_revision
    overlay = FulfillmentServicesBlock.approved_overlay_in_connection(conn)
    scope = json.loads(request["scope_json"])
    result = {}
    for identity, saved in sorted(scope.items()):
        if saved.get("cost_eligible") is False:
            continue
        source = intents.load_supply_in_connection(conn, identity)
        if source is None:
            raise ValueError("fulfillment_scope_pending")
        supply = _wb_supply_row_to_dict(source)
        supply_id = str(supply["supply_id"])
        goods = _parse_wb_goods(supply.get("raw_goods_json"))
        operands, denominator = {}, 0.0
        for good in goods:
            try:
                nm = int(good.get("nmID") or good.get("nmId") or good.get("nm_id"))
                packed_value = next(good[key] for key in ("quantity", "qty") if good.get(key) is not None)
                packed_decimal = Decimal(str(packed_value))
                accepted_value = next((good[key] for key in ("acceptedQuantity", "accepted_quantity")
                                       if good.get(key) is not None), None)
                accepted_decimal = Decimal(str(accepted_value)) if accepted_value is not None else None
                if (nm <= 0 or not packed_decimal.is_finite() or packed_decimal < 0
                        or accepted_decimal is not None and (not accepted_decimal.is_finite() or accepted_decimal < 0)):
                    raise ValueError()
            except (ValueError, TypeError, StopIteration, InvalidOperation):
                raise ValueError("fulfillment_supply_authority_incomplete") from None
            packed = _wb_good_packed_quantity(good)
            accepted = _wb_good_accepted_quantity(good)
            denominator += packed.qty
            if packed.qty > 0:
                # Match the existing native materializer's per-nm upsert order;
                # retain all raw rows separately so a composition edit is visible.
                operands[nm] = {"accepted_qty": accepted.qty,
                    "quantity_source": accepted.source,
                    "quantity_is_final_accepted": _wb_good_quantity_is_final_accepted(supply=supply, quantity=accepted)}
        if not operands or denominator <= 0:
            raise ValueError("fulfillment_supply_authority_incomplete")
        expected = overlay.get(supply_id) or {}
        layers = [dict(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers "
                                                   "WHERE wb_supply_id=? AND is_current=1", (supply_id,))]
        if (len(layers) != len(operands) or {layer["nm_id"] for layer in layers} != set(operands)
                or not set(operands) <= set(saved.get("nm_ids") or [])):
            raise ValueError("fulfillment_supply_sku_scope_changed")
        service = float(expected.get("service_amount_with_vat_without_storage_total") or 0)
        storage = float(expected.get("storage_allocated_amount_with_vat_total") or 0)
        def equal(actual, wanted):
            return actual is not None and isclose(float(actual), wanted, abs_tol=1e-8, rel_tol=0)
        for layer in layers:
            operand = operands[layer["nm_id"]]
            components = json.loads(layer["component_status_json"])
            # Reconstruct the native persisted input hash from its business
            # payload and the live full supply source. This also binds dates,
            # status and acceptance facts, beyond the FF amount checks below.
            payload = {key: layer[key] for key in (
                "wb_supply_id", "cache_key", "nm_id", "accepted_qty", "qty_denominator", "supply_date", "accepted_date",
                "supplier_ff_cost_layer_id", "supplier_ff_cost_layer_line_id", "sku_ff_unit_cost_rub",
                "transit_cost_status", "transit_amount_total", "transit_per_unit_rub", "ff_upload_id",
                "ff_services_amount_total", "ff_services_per_unit_rub", "ff_storage_amount_total", "ff_storage_per_unit_rub",
                "pre_acceptance_unit_cost_rub", "wb_acceptance_amount_total", "wb_acceptance_per_accepted_unit_rub",
                "our_wb_unit_cost_rub", "source_status", "missing_reason")}
            payload["component_status"] = components
            if layer["inputs_hash"] != _stable_hash({"schema": "wb_supply_cost_layer_v1", "payload": payload,
                                                      "supply": _selected_wb_supply_inputs(supply)}):
                raise ValueError("fulfillment_supply_cost_source_changed")
            if (set(filter(None, str(layer["ff_upload_id"] or "").split(","))) != set(expected.get("upload_ids") or [])
                    or not equal(layer["qty_denominator"], denominator)
                    or not equal(layer["accepted_qty"], operand["accepted_qty"])
                    or not equal(layer["ff_services_amount_total"], service)
                    or not equal(layer["ff_storage_amount_total"], storage)
                    or not equal(layer["ff_services_per_unit_rub"], service / denominator)
                    or not equal(layer["ff_storage_per_unit_rub"], storage / denominator)
                    or components.get("wb_quantity_source") != operand["quantity_source"]
                    or components.get("wb_quantity_final_accepted") != operand["quantity_is_final_accepted"]):
                raise ValueError("fulfillment_cost_materialization_pending")
            if layer["sku_ff_unit_cost_rub"] is not None and layer["transit_per_unit_rub"] is not None:
                pre = float(layer["sku_ff_unit_cost_rub"]) + float(layer["transit_per_unit_rub"]) + service / denominator + storage / denominator
                if (not equal(layer["pre_acceptance_unit_cost_rub"], pre)
                        or not equal(layer["our_wb_unit_cost_rub"], pre + float(layer["wb_acceptance_per_accepted_unit_rub"]))):
                    raise ValueError("fulfillment_cost_materialization_pending")
        result[supply_id] = {"upload_ids": sorted(expected.get("upload_ids") or []),
            "supply_authority": {"source_revision": _supply_revision(dict(source)),
                                 "goods": goods, "qty_denominator": denominator,
                                 "nm_ids": sorted(operands)},
            "layers": sorted(layers, key=canonical)}
    if not result:
        raise ValueError("fulfillment_scope_pending")
    return result


def _public(conn: sqlite3.Connection, row: Any) -> dict[str, Any]:
    source = json.loads(row["source_json"])
    current = _current(conn, row, source)
    request, queue = _request(conn, row)
    state, reason, native = "accepted", "derived_processing_pending", "pending"
    publication: dict[str, Any] = {"status": "pending"}
    if current != "current":
        state, reason, native = ("needs_attention" if current == "source_changed" else "accepted"), current, current
    elif request is None:
        state, reason, native = "delayed", "document_predates_atomic_delivery", "not_tracked"
    elif request["status"] == "not_applicable":
        reason, native = request["error"], "no_op"
        publication = {"status": "no_op", "terminal_no_op": True, "warehouse_mutation_count": 0}
    elif queue is None:
        state, reason = "delayed", "fulfillment_queue_delivery_pending"
    elif any(queue.get(key) for key in ("error", "economics_error", "finance_error")):
        state, reason = "delayed", "native_replay_requires_attention"
    elif queue["status"] in {"running", "complete"}:
        state, reason = "processing", "exact_publication_proof_pending"
    if queue:
        publication.update(queue_ref=_queue_ref(queue), queue_status=queue["status"],
                           economics_status=queue.get("economics_status") or "pending",
                           finance_status=queue.get("finance_status") or "pending")
    checked_at = row["accepted_at"]
    if current == "current" and native != "no_op" and _exists(conn, ATTEMPTS):
        attempt = conn.execute(f"SELECT * FROM {ATTEMPTS} WHERE operation_id=?",(row["operation_id"],)).fetchone()
        if attempt:
            state, reason, checked_at = attempt["state"], attempt["reason_code"], attempt["checked_at"]
    if current == "current" and _exists(conn, COMPLETIONS):
        completion = conn.execute(f"SELECT * FROM {COMPLETIONS} WHERE operation_id=?", (row["operation_id"],)).fetchone()
        if completion and digest(json.loads(completion["proof_json"])) == completion["proof_digest"]:
            state, reason, native = "completed", "", "complete"
            publication.update(status="complete", completed_at=completion["completed_at"],
                               proof_digest=completion["proof_digest"], proof=json.loads(completion["proof_json"]))
    if current != "current":
        publication["status"] = current
    reasons = {"derived_processing_pending": "Документ сохранён. Расчёт выполняет штатная обработка.",
        "fulfillment_queue_delivery_pending": "Документ сохранён. Ожидает подтверждённой области расчёта.",
        "exact_publication_proof_pending": "Документ сохранён. Ожидает подтверждения публикации этой версии.",
        "document_predates_atomic_delivery": "Для этого документа нет подтверждения обработки версии.",
        "native_replay_requires_attention": "Документ сохранён. Обработка отложена; повторный ввод не требуется.",
        "source_changed": "Сохранённый источник изменился. Требуется разбор.",
        "superseded": "Загрузка исключена связанным действием удаления.",
        "fulfillment_supply_outside_current_cost_window": "Документ сохранён. Вне текущей области расчёта стоимости."}
    return {"domain": DOMAIN, "operation_id": row["operation_id"], "durable_saved": True,
        "accepted_at": row["accepted_at"], "updated_at": publication.get("completed_at") or checked_at,
        "actor": row["actor"], "state": state, "native_state": native, "primary_effect": "source_saved",
        "source_ref": _ref(row, source), "source_complete": native == "no_op" or state == "completed",
        "processing_not_applicable": native == "no_op", "reason_code": reason, "reason_ru": reasons.get(reason, "Документ сохранён. Ожидает штатного обновления связанных расчётов." if state == "processing" else "Документ сохранён. Связанные расчёты требуют проверки." if state in {"delayed","needs_attention"} else ""),
        "title_ru": "Удаление документа услуг ФФ" if row["action"] == "delete" else "Услуги и хранение ФФ",
        "fields": [{"label": "Файл", "value": source["header"]["original_filename"]},
                   {"label": "Сумма с НДС, ₽", "value": source["header"]["amount_with_vat_total"]}],
        "publication": publication,
        "journal_path": "/sheet-vitrina-v1/operations?operation_id=" + row["operation_id"],
        "detail_path": "/v1/sheet-vitrina-v1/operations/" + row["operation_id"]}


def read_acceptance(db_path: Path | str, operation_id: str) -> dict[str, Any] | None:
    with closing(readonly(db_path)) as conn:
        if not _exists(conn, TABLE):
            return None
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (operation_id,)).fetchone()
        return _public(conn, row) if row else None


def read_source_acceptance(db_path: Path | str, upload_id: str, *, action: str,
                           source_revision: str) -> dict[str, Any] | None:
    with closing(readonly(db_path)) as conn:
        if not _exists(conn, TABLE):
            return None
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE upload_id=? AND action=? AND source_revision=?",
                           (upload_id, action, source_revision)).fetchone()
        return _public(conn, row) if row else None


def journal(db_path: Path | str, *, page: int = 1, limit: int = 25) -> dict[str, Any]:
    # Internal domain projection; host must authorize/filter before cross-domain totals.
    if type(page) is not int or type(limit) is not int or not 1 <= page <= 100000 or not 1 <= limit <= 100:
        raise ValueError("invalid_fulfillment_journal_page")
    with closing(readonly(db_path)) as conn:
        if not _exists(conn, TABLE):
            return {"items": [], "total": 0, "page": page, "limit": limit, "has_more": False}
        total = int(conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
        rows = conn.execute(f"SELECT * FROM {TABLE} ORDER BY accepted_at DESC,operation_id DESC LIMIT ? OFFSET ?",
                            (limit, (page - 1) * limit)).fetchall()
        return {"items": [_public(conn, row) for row in rows], "total": total,
                "page": page, "limit": limit, "has_more": page * limit < total}


def record_functional_publication(conn: sqlite3.Connection, *, plan: dict[str, Any], version_id: str) -> None:
    """Native publisher hook in its source-CAS transaction, not a new runner.

    Bind each exact accepted action to the version that actually consumed it.
    Legacy sources without a receipt are ignored; no history backfill is created.
    """
    if not conn.in_transaction:
        raise ValueError("fulfillment_publication_requires_native_transaction")
    if not _exists(conn, TABLE) or not _exists(conn, FUNCTIONAL_PROOFS):
        return
    candidates = {}
    for queued in plan.get("targeted_recalc_requests") or []:
        stable = str(queued.get("stable_source_id") or "")
        if not stable.startswith("fulfillment_upload:"):
            continue
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE upload_id=? AND source_revision=?",
                           (stable.removeprefix("fulfillment_upload:"), queued["source_revision"])).fetchone()
        if row is None:
            continue
        candidates[row["operation_id"]] = row
    # Advance only already correlated, unfinished actions. A later native CAS
    # can coalesce multiple accepted uploads before their ready/Finance stages.
    # No source scan, legacy receipt backfill, or new demand is created here.
    for row in conn.execute(f"SELECT DISTINCT receipt.* FROM {TABLE} receipt "
            f"JOIN {FUNCTIONAL_PROOFS} proof USING(operation_id) LEFT JOIN {COMPLETIONS} done USING(operation_id) "
            "WHERE done.operation_id IS NULL"):
        candidates[row["operation_id"]] = row
    covered = set((plan.get("wb_snapshot") or {}).get("requested_nm_ids") or [])
    for row in candidates.values():
        source = json.loads(row["source_json"])
        if _current(conn, row, source) != "current":
            continue  # A coalesced upload+delete publishes the current deletion.
        request, queue = _request(conn, row)
        if not request or not queue or queue["status"] != "complete":
            raise ValueError("fulfillment_native_publication_queue_mismatch")
        if not set(_queue_ref(queue)["affected_nm_ids"]) <= covered:
            continue
        proof = {"source_digest": row["source_digest"], "source_ref": _ref(row, source),
                 "queue_ref": _queue_ref(queue), "warehouse_version_id": version_id,
                 "plan_fingerprint": plan["plan_fingerprint"],
                 "business_date": plan["effective_date"], "overlay": _overlay_evidence(conn, request)}
        ids = proof["queue_ref"]["affected_nm_ids"]
        proof["daily_cost_rows"] = [dict(item) for item in conn.execute(
            "SELECT as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,fingerprint "
            "FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=? AND as_of_date>=? AND as_of_date<=? "
            "AND nm_id IN ("+','.join('?' for _ in ids)+") ORDER BY as_of_date,nm_id",
            ('warehouse_functional_cutover_v1',queue["effective_date"],plan["effective_date"],*ids))]
        existing = conn.execute(f"SELECT proof_json FROM {FUNCTIONAL_PROOFS} WHERE operation_id=? AND warehouse_version_id=?", (row["operation_id"],version_id)).fetchone()
        if existing and canonical(json.loads(existing[0])) != canonical(proof):
            raise ValueError("fulfillment_native_publication_conflict")
        conn.execute(f"INSERT OR IGNORE INTO {FUNCTIONAL_PROOFS} VALUES(?,?,?,?)",
                     (row["operation_id"], version_id, canonical(proof), digest(proof)))


def _after_native_proof():
    """Deterministic disposable-fixture handoff boundary; production is inert."""


def record_completion(runtime: Any, operation_id: str, *, seller_id: str = "canonical", now=None) -> dict[str, Any]:
    """Native owner finalizer; heavy RO proof precedes a short receipt CAS.

    No caller evidence or callback. Keep the original RO observers alive through
    writer admission, end their read transactions before each data_version fence,
    and verify source/queue/ready/book identities again under the native lock.
    """
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    from packages.application.operator_fulfillment_services_proof import read_native_proof
    require_heavy_owner(runtime.runtime_dir)
    require_warehouse_job_owner(runtime.runtime_dir)
    with ExitStack() as stack:
        observer = stack.enter_context(closing(readonly(runtime.db_path)))
        row = observer.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("fulfillment_receipt_missing")
        if _current(observer, row, json.loads(row["source_json"])) != "current":
            raise ValueError("fulfillment_completion_source_changed")
        existing = observer.execute(f"SELECT * FROM {COMPLETIONS} WHERE operation_id=?", (operation_id,)).fetchone()
        if existing:
            if digest(json.loads(existing["proof_json"])) != existing["proof_digest"]:
                raise ValueError("fulfillment_completion_receipt_corrupt")
            return _public(observer, row)  # immutable past completion, not latest-book churn
        # Restart the observer before capturing the proof's snapshot and token.
        observer.commit()
        observer.execute("BEGIN")
        handoff = []
        proof = read_native_proof(runtime, operation_id, seller_id=seller_id, now=now,
                                  connection=observer, _handoff=handoff, _stack=stack)
        _after_native_proof()
        with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5), closing(sqlite3.connect(runtime.db_path, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or _current(conn, row, json.loads(row["source_json"])) != "current":
                raise ValueError("fulfillment_completion_source_changed")
            source_ref = _ref(row, json.loads(row["source_json"]))
            if source_ref != proof["source_ref"]:
                raise ValueError("fulfillment_completion_identity_mismatch")
            if any(observe(live) != token for live, observe, token in handoff):
                raise ValueError("fulfillment_completion_handoff_changed")
            request, queue = _request(conn, row)
            if proof["status"] == "not_applicable":
                if not request or request["status"] != "not_applicable" or digest(json.loads(request["scope_json"])) != proof["scope_digest"]:
                    raise ValueError("fulfillment_completion_scope_changed")
            else:
                functional = proof["functional"]
                saved = conn.execute(f"SELECT proof_digest FROM {FUNCTIONAL_PROOFS} WHERE operation_id=? AND warehouse_version_id=?",
                                     (operation_id,functional["warehouse_version_id"])).fetchone()
                if not saved or saved[0] != digest(functional) or not queue or _queue_ref(queue) != functional["queue_ref"]:
                    raise ValueError("fulfillment_completion_native_identity_changed")
                publication = proof["publication"]
                published = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",
                    (publication["operation_id"],publication["attempt_id"])).fetchone()
                ready = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                    (publication["bundle_version"],publication["ready_as_of_date"])).fetchone()
                from packages.application.ready_publication import digest as ready_digest
                book_observer = handoff[-1][0]
                pointer = book_observer.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()
                if (not published or published["state"] != "complete" or published["book_version"] != publication["book_version"]
                        or published["after_digest"] != publication["after_digest"] or not ready
                        or ready_digest(ready[0]) != publication["after_digest"]
                        or not pointer or pointer[0] != publication["book_version"]):
                    raise ValueError("fulfillment_completion_publication_changed")
                # Also fence the separate raw/book files immediately before commit.
                if any(observe(live) != token for live, observe, token in handoff):
                    raise ValueError("fulfillment_completion_handoff_changed")
                conn.execute(f"INSERT OR IGNORE INTO {COMPLETIONS} VALUES(?,?,?,?)", (
                    operation_id, canonical(proof), digest(proof), datetime.now(timezone.utc).isoformat()))
            conn.commit()
    return read_acceptance(runtime.db_path, operation_id)


def reconcile(runtime, *, seller_id="canonical", now=None):
    """Existing native owner calls this after accounting/ready and Finance.

    Capture only accepted unfinished receipts, never backfill uploads or enqueue
    new work. Each real verifier failure stays pending with a visible reason.
    """
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    require_heavy_owner(runtime.runtime_dir)
    require_warehouse_job_owner(runtime.runtime_dir)
    with closing(readonly(runtime.db_path)) as conn:
        rows = conn.execute(f"SELECT source.* FROM {TABLE} source LEFT JOIN {COMPLETIONS} done USING(operation_id) "
                            "WHERE done.operation_id IS NULL ORDER BY accepted_at,operation_id").fetchall() if _exists(conn,TABLE) else []
        current = [row["operation_id"] for row in rows if _current(conn,row,json.loads(row["source_json"])) == "current"]
    results = []
    for operation_id in current:
        try:
            value = record_completion(runtime, operation_id, seller_id=seller_id, now=now)
            results.append({"operation_id": operation_id, "state": value["state"], "source_complete": value["source_complete"]})
        except (ValueError, sqlite3.OperationalError) as exc:
            reason = str(exc).split(":")[0]
            state = "needs_attention" if reason in {"fulfillment_supply_sku_scope_changed", "fulfillment_supply_authority_incomplete"} else "delayed"
            if reason.endswith(("_changed", "_pending", "_missing")) and state != "needs_attention":
                state = "processing"
            # Small status write only; the heavy verifier has already released
            # its readers. Never overwrite an immutable completion.
            with warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=5), closing(sqlite3.connect(runtime.db_path,timeout=2)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not conn.execute(f"SELECT 1 FROM {COMPLETIONS} WHERE operation_id=?",(operation_id,)).fetchone():
                    conn.execute(f"INSERT OR REPLACE INTO {ATTEMPTS} VALUES(?,?,?,?)",(
                        operation_id,datetime.now(timezone.utc).isoformat(),state,reason))
                conn.commit()
            results.append({"operation_id": operation_id, "state": "pending", "reason_code": reason})
    return {"status": "pending" if any(item["state"]=="pending" for item in results) else "ok", "operations": results}
