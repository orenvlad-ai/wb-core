"""Confirmed FF expense receipts and their bounded, query-only operation journal.

Preview requests remain previews. A confirmation owns a frozen source receipt;
posting and dated accounting publication remain separately evidenced effects.
"""
from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3

TABLE = "sheet_vitrina_v1_ff_pool_overhead_confirmations"
PREFIX = "/v1/sheet-vitrina-v1/operations"
NATIVE = "/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/requests/"
COMPLETION_REQUEST_COLUMNS = 'request_id,document_kind,request_payload_json,idempotency_epoch,business_date,source_revision,source_sha256,request_identity,posted_document_id,posted_manifest_sha256,actor,recovery_operation_id'
STATES = ("accepted", "processing", "completed", "delayed", "needs_attention")


def ensure_schema(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TABLE}(
            request_id TEXT PRIMARY KEY REFERENCES sheet_vitrina_v1_ff_pool_document_requests(request_id),
            source_json TEXT NOT NULL CHECK(json_valid(source_json)),
            source_digest TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('accepted','processing','completed','delayed','needs_attention')),
            updated_at TEXT NOT NULL,
            reason_code TEXT NOT NULL DEFAULT '',
            receipt_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(receipt_json))
        );
        CREATE INDEX IF NOT EXISTS ff_overhead_confirmations_by_time ON {TABLE}(accepted_at,request_id);
        CREATE TRIGGER IF NOT EXISTS ff_overhead_superseded_preview_cannot_confirm
        BEFORE INSERT ON {TABLE}
        WHEN EXISTS(SELECT 1 FROM sheet_vitrina_v1_ff_pool_overhead_payment_renewals WHERE predecessor_request_id=NEW.request_id)
        BEGIN SELECT RAISE(ABORT,'superseded overhead preview cannot confirm'); END;

        CREATE TRIGGER IF NOT EXISTS ff_overhead_confirmation_source_immutable
        BEFORE UPDATE ON {TABLE}
        WHEN NEW.request_id<>OLD.request_id OR NEW.source_json<>OLD.source_json
          OR NEW.source_digest<>OLD.source_digest OR NEW.accepted_at<>OLD.accepted_at OR NEW.actor<>OLD.actor
        BEGIN SELECT RAISE(ABORT,'confirmed FF overhead source is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_overhead_confirmation_no_delete
        BEFORE DELETE ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'confirmed FF overhead source is append-only'); END;
    """)
    conn.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS ff_overhead_confirmed_request_source_immutable
        BEFORE UPDATE ON sheet_vitrina_v1_ff_pool_document_requests
        WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE request_id=OLD.request_id)
         AND (NEW.request_id IS NOT OLD.request_id OR NEW.request_payload_json IS NOT OLD.request_payload_json
          OR NEW.request_identity IS NOT OLD.request_identity OR NEW.business_date IS NOT OLD.business_date
          OR NEW.source_revision IS NOT OLD.source_revision OR NEW.source_sha256 IS NOT OLD.source_sha256
          OR NEW.source_file_blob IS NOT OLD.source_file_blob OR NEW.source_filename IS NOT OLD.source_filename
          OR NEW.source_content_type IS NOT OLD.source_content_type OR NEW.source_id IS NOT OLD.source_id
          OR NEW.source_type IS NOT OLD.source_type OR NEW.source_system IS NOT OLD.source_system
          OR NEW.idempotency_epoch IS NOT OLD.idempotency_epoch OR NEW.actor IS NOT OLD.actor
          OR NEW.document_kind IS NOT OLD.document_kind)
        BEGIN SELECT RAISE(ABORT,'confirmed FF overhead request source is immutable'); END;
    """)


def readonly(db_path):
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def _resolve(conn, identity):
    row = conn.execute("SELECT request_id FROM sheet_vitrina_v1_ff_pool_document_request_aliases WHERE client_request_id=?", (identity,)).fetchone()
    return str(row[0]) if row else identity


def _public(conn, row):
    from packages.contracts.ff_pool_documents import OVERHEAD_EXPENSE_CATEGORY_LABELS_RU
    from packages.application.ff_pool_documents import REQUESTS_TABLE, _pool_overhead_publication_state

    source = json.loads(row["source_json"])
    manifest = source["manifest"]
    request = conn.execute(f"SELECT posted_document_id,state FROM {REQUESTS_TABLE} WHERE request_id=?", (row["request_id"],)).fetchone()
    doc_id = str(request["posted_document_id"] or "") if request else ""
    publication = _pool_overhead_publication_state(conn, document_id=doc_id) if doc_id else None
    if publication:
        publication = {k:v for k,v in publication.items() if k not in {"error", "affected_nm_ids"}}
    state = row["state"]
    labels = {"accepted": "Принято", "processing": "Обрабатывается", "completed": "Обработано",
              "delayed": "Обработка отложена", "needs_attention": "Требует внимания"}
    reasons = {
        "": "Документ принят. Повторный ввод не требуется.",
        "warehouse_busy": "Складской расчёт занят. Документ сохранён для следующего цикла.",
        "accounting_unavailable": "Ожидает доступного расчёта себестоимости за дату документа.",
        "native_post_pending": "Документ сохранён. Проведение ожидает следующего цикла.",
        "closed_accounting_day": "Дата документа уже закрыта в учёте. Требуется разбор без изменения даты.",
        "source_changed": "Сохранённый источник изменился. Требуется разбор.",
        "posting_blocked": "Проведение заблокировано. Документ сохранён и требует разбора.",
        "publication_pending": "Документ проведён. Ожидает подтверждения расчёта себестоимости.",
    }
    summary = {key: manifest.get(key, "") for key in ("facility_id", "scope", "amount_rub", "category", "comment", "source_mode")}
    summary.update(facility_name=source["facility_name"], category_label_ru=OVERHEAD_EXPENSE_CATEGORY_LABELS_RU.get(manifest["category"], ""))
    return {"durable_saved": True, "operation_id": row["request_id"], "request_id": row["request_id"],
        "accepted_at": row["accepted_at"], "state": state, "label_ru": labels[state],
        "reason_ru": reasons.get(row["reason_code"], reasons["posting_blocked"]), "reason_code": row["reason_code"],
        "business_date": source["business_date"], "summary": summary,
        "source_document": {"request_id": row["request_id"], "source_sha256": source["source_sha256"],
            "source_revision": source["source_revision"], "filename": source["filename"], "source_mode": manifest["source_mode"]},
        "document": {"document_id": doc_id, "posted": True} if doc_id else None,
        "publication": publication, "processing_receipt": json.loads(row["receipt_json"]),
        "journal_path": "/sheet-vitrina-v1/operations?operation_id=" + row["request_id"],
        "detail_path": PREFIX + "/" + row["request_id"], "native_detail_path": NATIVE + row["request_id"]}


def read_acceptance(db_path, identity):
    """Exact native ID or saved client alias. No bootstrap, resume or writes."""
    with closing(readonly(db_path)) as conn:
        if not _exists(conn):
            return None
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_id=?", (_resolve(conn, identity),)).fetchone()
        return _public(conn, row) if row else None


def journal(db_path, *, page=1, limit=25):
    if isinstance(page, bool) or isinstance(limit, bool) or not 1 <= page <= 100000 or not 1 <= limit <= 100:
        raise ValueError("invalid_operation_journal_page")
    with closing(readonly(db_path)) as conn:
        total = int(conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]) if _exists(conn) else 0
        rows = conn.execute(f"SELECT * FROM {TABLE} ORDER BY accepted_at DESC,request_id DESC LIMIT ? OFFSET ?", (limit, (page-1)*limit)).fetchall() if total else []
        items = [_public(conn, row) for row in rows]
    return {"contract_name": "operator_operations_v1", "status": "ready", "items": items,
            "page": page, "limit": limit, "total": total, "has_more": page * limit < total}


def confirm_source(db_path, identity, *, now, current_day, actor=None, runtime_dir=None, timestamp_factory=None):
    """Commit intrinsic source validation and the final confirmation together.

    Existing requests already own the original bytes. The immutable snapshot
    below binds those exact bytes/metadata to final intent in one transaction.
    No allocation preview or warehouse publication is a prerequisite.
    """
    from packages.application.ff_pool_documents import (
        REQUESTS_TABLE, _connect, _writer_epoch, _facility, _scope, _money_cents,
        _overhead_metadata, _fingerprint, FfPoolDocumentError,
    )
    from packages.application.warehouse_domain_write_guard import assert_warehouse_domain_write_allowed

    with closing(_connect(db_path)) as conn:
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("BEGIN IMMEDIATE")
        assert_warehouse_domain_write_allowed(conn, writer="ff_overhead_confirmation")
        canonical = _resolve(conn, identity)
        existing = conn.execute(f"SELECT * FROM {TABLE} WHERE request_id=?", (canonical,)).fetchone()
        if existing is not None:
            conn.rollback()
            return canonical
        request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (canonical,)).fetchone()
        if request is None or request["document_kind"] != "pool_overhead":
            raise FfPoolDocumentError("overhead_request_not_found", "Сохранённый источник расхода не найден")
        manifest = json.loads(request["request_payload_json"])
        epoch = _writer_epoch(conn)
        if epoch != request["idempotency_epoch"]:
            raise FfPoolDocumentError("feature_epoch_changed", "Складской режим изменился после проверки")
        facility = _facility(conn, manifest["facility_id"], require_active=True)
        from packages.application.ff_pool_surfaces import _business_date_in_timezone
        timezone_name = conn.execute("SELECT display_timezone FROM sheet_vitrina_v1_ff_facilities WHERE facility_id=?", (facility,)).fetchone()[0]
        # Clock is sampled after obtaining the source transaction, never before a lock wait.
        now = timestamp_factory() if timestamp_factory is not None else now
        current_day = _business_date_in_timezone(now, timezone_name)
        _scope(manifest["scope"])
        _overhead_metadata(conn, request=request, manifest=manifest,
            amount_cents=_money_cents(manifest["amount_rub"], field="overhead amount", positive=True))
        if request["business_date"] != current_day:
            if manifest["source_mode"] == "payment_order_pdf":
                raise FfPoolDocumentError("overhead_unconfirmed_payment_prior_day", stale_payment_reason(request["business_date"]))
            raise FfPoolDocumentError("overhead_business_date_stale", "Дата выбранного склада изменилась после проверки. Выполните проверку заново.")
        if runtime_dir is not None and _closed_day(runtime_dir, request["business_date"]):
            raise FfPoolDocumentError("closed_accounting_day", "Дата документа уже закрыта в учёте")
        name = conn.execute("SELECT name FROM sheet_vitrina_v1_ff_facilities WHERE facility_id=?", (facility,)).fetchone()[0]
        source = {"manifest": manifest, "business_date": request["business_date"], "facility_name": name,
                  "source_revision": request["source_revision"], "source_sha256": request["source_sha256"],
                  "filename": request["source_filename"], "feature_epoch": epoch,
                  "request_identity": request["request_identity"]}
        serialized = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(f"INSERT INTO {TABLE}(request_id,source_json,source_digest,accepted_at,actor,state,updated_at) VALUES(?,?,?,?,?,'accepted',?)",
                     (canonical, serialized, _fingerprint(source), now, actor or request["actor"], now))
        conn.commit()
    return canonical


def source_confirmable(db_path, identity):
    """Read intrinsic guards only; failed derived allocation is not rejection."""
    from packages.application.ff_pool_documents import REQUESTS_TABLE, _writer_epoch, _facility, _scope, _money_cents, _overhead_metadata
    try:
        with closing(readonly(db_path)) as conn:
            row = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (_resolve(conn, identity),)).fetchone()
            if row is None or row["document_kind"] != "pool_overhead" or row["posted_document_id"]:
                return False
            manifest = json.loads(row["request_payload_json"])
            if _writer_epoch(conn) != row["idempotency_epoch"]:
                return False
            _facility(conn, manifest["facility_id"], require_active=True)
            _scope(manifest["scope"])
            _overhead_metadata(conn, request=row, manifest=manifest,
                amount_cents=_money_cents(manifest["amount_rub"], field="overhead amount", positive=True))
            return True
    except (ValueError, RuntimeError):
        return False


def assert_day_can_close(conn, day):
    """This same pinned source transaction must see every accepted day's intent."""
    if _exists(conn) and conn.execute(f"""SELECT 1 FROM {TABLE} c
            JOIN sheet_vitrina_v1_ff_pool_document_requests r USING(request_id)
            WHERE json_extract(c.source_json,'$.business_date')<=?
              AND r.posted_document_id='' LIMIT 1""", (day,)).fetchone():
        raise ValueError("confirmed_overhead_day_pending")


def _update(db_path, identity, state, reason, receipt=None, *, guard=None):
    if guard is not None: guard()
    with closing(sqlite3.connect(db_path, timeout=2)) as conn, conn:
        conn.row_factory=sqlite3.Row
        if guard is not None:
            initial_changes=conn.total_changes
            conn.execute("BEGIN IMMEDIATE")
            transaction=guard(conn,begin=True,initial_changes=initial_changes)
        previous=conn.execute(f'SELECT receipt_json FROM {TABLE} WHERE request_id=?',(identity,)).fetchone()
        retained=json.loads(previous[0]).get('native_completion') if previous else None
        if retained and not (receipt or {}).get('native_completion'):
            receipt={**(receipt or {}),'native_completion':retained}
        conn.execute(f"UPDATE {TABLE} SET state=?,reason_code=?,updated_at=?,receipt_json=? WHERE request_id=?",
                     (state, reason, datetime.now(timezone.utc).isoformat(), json.dumps(receipt or {}, ensure_ascii=False), identity))
        if guard is not None: guard(conn,transaction=transaction)


def _closed_day(runtime_dir, day):
    from packages.application.fbs_accounting_runtime import load
    book, _version = load(runtime_dir)
    if not book or not book["active"]:
        return False
    closed = [d for d,p in book["state"]["periods"].items() if p["status"] == "closed"]
    return bool(closed and day <= max(closed))


def drain(db_path, runtime_dir, *, limit=100, timestamp_factory=None):
    """One consumer, native idempotent post, frozen source, retryable derived state.

    A technical legacy plan is refreshed under the native posting owner. It is
    never the daily accounting allocation: the latter consumes dated header
    facility/scope/amount and publishes its own operand/receipt.
    """
    from packages.application.ff_pool_documents import (FfPoolDocumentService, REQUESTS_TABLE,
        _connect, _writer_epoch, _build_posting_plan, _pool_overhead_plan_preview, _fingerprint, FfPoolDocumentError)
    from packages.application.warehouse_functional_lock import (warehouse_functional_write_lock, WarehouseFunctionalBusyError)
    runtime_dir = Path(runtime_dir)
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    require_warehouse_job_owner(runtime_dir)
    with closing(readonly(db_path)) as conn:
        if not _exists(conn):
            return {"processed_count": 0}
        rows = conn.execute(f"""SELECT c.* FROM {TABLE} c JOIN sheet_vitrina_v1_ff_pool_document_requests r USING(request_id)
            WHERE c.state NOT IN ('completed','needs_attention')
            ORDER BY (r.posted_document_id!=''),c.accepted_at,c.request_id LIMIT ?""", (limit,)).fetchall()
    # Match native posting lock order: posting owner, then warehouse writer.
    with (runtime_dir / ".ff-pool-document-posting.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"processed_count": 0, "status": "busy"}
        processed = 0
        try:
            service = FfPoolDocumentService(db_path=Path(db_path), runtime_dir=runtime_dir, resume=False, bootstrap=False, timestamp_factory=timestamp_factory)
            for row in rows:
                identity = row["request_id"]
                try:
                    with warehouse_functional_write_lock(runtime_dir, timeout_seconds=5):
                        source = json.loads(row["source_json"])
                        with closing(_connect(Path(db_path), query_only=True)) as conn:
                            request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (identity,)).fetchone()
                            if (_fingerprint(source) != row["source_digest"] or request is None
                                    or json.loads(request["request_payload_json"]) != source["manifest"]
                                    or any(request[key] != source[key] for key in ("business_date", "source_revision", "source_sha256", "request_identity"))):
                                _update(db_path, identity, "needs_attention", "source_changed")
                                continue
                            if not request["posted_document_id"]:
                                if _closed_day(runtime_dir, source["business_date"]):
                                    _update(db_path, identity, "needs_attention", "closed_accounting_day")
                                    continue
                                epoch = _writer_epoch(conn)
                                if epoch != source["feature_epoch"]:
                                    raise FfPoolDocumentError("feature_epoch_changed", "Confirmed source belongs to another epoch")
                                plan = _build_posting_plan(conn, request=request, manifest=source["manifest"], epoch=epoch)
                                preview = {**source["manifest"], "posting_plan_preview": _pool_overhead_plan_preview(plan=plan, epoch=epoch)}
                            else:
                                preview = None
                        _update(db_path, identity, "processing", "native_post_pending")
                        if preview is not None:
                            with closing(_connect(Path(db_path))) as conn, conn:
                                conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='ready',preview_manifest_json=?,error_code='',error_details_json='null' WHERE request_id=? AND posted_document_id=''",
                                             (json.dumps(preview, ensure_ascii=False), identity))
                            # Already hold both owners; native method retains every posting guard/T1 recovery.
                            service._post_once_under_writer_lock(identity, defer_confirmed_overhead_projection=True)
                        with closing(_connect(Path(db_path), query_only=True)) as conn:
                            request = conn.execute(f"SELECT posted_document_id FROM {REQUESTS_TABLE} WHERE request_id=?", (identity,)).fetchone()
                        if not request[0]:
                            raise ValueError("native_post_missing")
                    _update(db_path, identity, "processing", "publication_pending", {"document_id": request[0], "native_posted": True})
                    processed += 1
                except WarehouseFunctionalBusyError:
                    _update(db_path, identity, "delayed", "warehouse_busy")
                except Exception as exc:
                    code = getattr(exc, "code", "")
                    permanent = code in {"feature_epoch_changed", "feature_writer_disabled", "unknown_or_inactive_facility", "overhead_amount_mismatch", "overhead_payment_evidence_mismatch", "overhead_payment_order_not_eligible"}
                    _update(db_path, identity, "needs_attention" if permanent else "delayed", "posting_blocked" if permanent else "accounting_unavailable", {"error_code": code or type(exc).__name__})
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {"processed_count": processed, "status": "ready"}


def reconcile(runtime, *, now=None):
    """Complete exact publications only after native readback and T1 retention."""
    from packages.application.fbs_overhead_presentation import OverheadAccountingView
    from packages.application.fbs_accounting_runtime import current_publication_receipt
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    require_warehouse_job_owner(runtime.runtime_dir)
    view = OverheadAccountingView(runtime.runtime_dir, runtime.db_path, now=now)
    with closing(readonly(runtime.db_path)) as conn:
        if not _exists(conn):
            return {"processed_count": 0}
        rows = conn.execute(f"SELECT * FROM {TABLE} WHERE state IN ('accepted','processing','delayed') ORDER BY accepted_at,request_id LIMIT 100").fetchall()
        receipts = [_public(conn, row) for row in rows]
    count = 0
    for acceptance in receipts:
        if requires_current_completion(runtime.db_path,acceptance["request_id"]): continue
        document = acceptance["document"]
        if not document:
            continue
        allocation = view.resolve(acceptance["summary"], day=acceptance["business_date"], document_id=document["document_id"], posted=True)
        native = acceptance["publication"] or {}
        try:
            publication = current_publication_receipt(runtime, now=now)
        except (ValueError, sqlite3.Error):
            publication = None
        if (allocation and allocation["allocation_status"] == "published" and publication
                and allocation["accounting_version"] == publication["accounting_version"]
                and native.get("status") == "complete"):
            try:
                recovery = _finalize_confirmed(runtime, acceptance)
            except Exception as exc:
                # Retain/native completion may commit before a lost response.
                # Leave the intent retryable; the native finalizer is idempotent
                # and never posts this confirmed overhead source again.
                _update(runtime.db_path, acceptance["request_id"], "delayed", "accounting_unavailable",
                        {"error_code": getattr(exc, "code", "") or type(exc).__name__})
                continue
            _update(runtime.db_path, acceptance["request_id"], "completed", "", {
                "document_id": document["document_id"], "native_posted": True,
                "business_date": acceptance["business_date"], "canonical_allocation": {k:v for k,v in allocation.items() if k != "lines"},
                "accounting_publication": publication, "native_publication": native,
                "native_state": "complete", "native_recovery": recovery})
            count += 1
    return {"processed_count": count}


def _finalize_confirmed(runtime, acceptance, *, prepare_guard=None, finish=None):
    """Retain only this posted source, under the same owners as native posting."""
    from packages.application.ff_pool_documents import FfPoolDocumentService, REQUESTS_TABLE, _fingerprint
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    require_warehouse_job_owner(runtime.runtime_dir)
    identity = acceptance["request_id"]
    if prepare_guard is None and requires_current_completion(runtime.db_path,identity):
        raise ValueError("overhead_completion_current_guard_required")
    with (Path(runtime.runtime_dir) / ".ff-pool-document-posting.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5):
                from packages.application.fbs_accounting_runtime import writer_lock
                with (writer_lock(runtime.runtime_dir) if prepare_guard else nullcontext()):
                    guard = prepare_guard() if prepare_guard else None
                    with closing(readonly(runtime.db_path)) as conn:
                        request = conn.execute(f"SELECT {COMPLETION_REQUEST_COLUMNS} FROM {REQUESTS_TABLE} WHERE request_id=?", (identity,)).fetchone()
                        assert_native_confirmation(conn, request)
                        if request["posted_document_id"] != acceptance["document"]["document_id"]:
                            raise ValueError("confirmed_overhead_document_changed")
                    service = FfPoolDocumentService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
                                                   resume=False, bootstrap=False)
                    service._finalize_posted(identity, **({"completion_guard": guard} if guard else {}))
                    readback = service._verify_posted_readback(identity)
                    with closing(readonly(runtime.db_path)) as conn:
                        row = conn.execute(f"""SELECT r.state,recovery.operation_id,recovery.lifecycle_state,recovery.after_digest
                            FROM {REQUESTS_TABLE} r LEFT JOIN sheet_vitrina_v1_recovery_operations recovery
                            ON recovery.operation_id=r.recovery_operation_id WHERE r.request_id=?""", (identity,)).fetchone()
                        if (row["state"] != "complete" or row["lifecycle_state"] != "retained"
                                or row["after_digest"] != _fingerprint(readback)):
                            raise ValueError("confirmed_overhead_native_not_terminal")
                        recovery = {"operation_id": row["operation_id"], "lifecycle": row["lifecycle_state"],
                                "after_digest": row["after_digest"]}
                    if finish is not None: finish(recovery, guard)
                    return recovery
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def assert_book_closure(conn, book):
    """Check only closing-day dependencies inside the source commit transaction.

    New current-day confirmations after this cycle's cutoff belong to the next
    cycle and do not invalidate its current publication. The source transaction
    also excludes a racing acceptance while a closed book is committed.
    """
    if not conn.in_transaction:
        raise ValueError("overhead_closure_requires_source_transaction")
    if book and book.get("active"):
        closed = [d for d,p in book["state"]["periods"].items() if p["status"] == "closed"]
        if closed:
            assert_day_can_close(conn, max(closed))


def assert_native_confirmation(conn, request):
    """Private cycle exception requires the exact immutable accepted command."""
    from packages.application.ff_pool_documents import _fingerprint, FfPoolDocumentError
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE request_id=?", (request["request_id"],)).fetchone()
    source = json.loads(row["source_json"]) if row else {}
    if (row is None or request["document_kind"] != "pool_overhead"
            or _fingerprint(source) != row["source_digest"]
            or json.loads(request["request_payload_json"]) != source["manifest"]
            or request["idempotency_epoch"] != source["feature_epoch"]
            or any(request[key] != source[key] for key in ("business_date", "source_revision", "source_sha256", "request_identity"))):
        raise FfPoolDocumentError("confirmed_overhead_source_required", "Exact confirmed overhead source required")


def stale_payment_reason(day):
    return (f"Этот платёж уже загружали {day}, но не подтвердили. "
            "Загрузите этот PDF ещё раз с прежними параметрами расхода, чтобы проверить и подтвердить его текущей датой учёта склада.")


def _completion_source(conn, identity):
    """Original posted operands, including the deterministic queue, never today's pools."""
    from decimal import Decimal
    import hashlib
    from packages.application.ff_pool_documents import REQUESTS_TABLE, DOCUMENTS_TABLE, TARGETED_RECALC_QUEUE_TABLE, _fingerprint, _json
    row = conn.execute(f'SELECT * FROM {TABLE} WHERE request_id=?', (identity,)).fetchone()
    request = conn.execute(f'SELECT {COMPLETION_REQUEST_COLUMNS} FROM {REQUESTS_TABLE} WHERE request_id=?', (identity,)).fetchone()
    if row is None or request is None or not request['posted_document_id']:
        raise ValueError('overhead_completion_source_missing')
    assert_native_confirmation(conn, request)
    document = conn.execute(f'SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=? AND request_id=?',
                            (request['posted_document_id'], identity)).fetchone()
    if document is None:
        raise ValueError('overhead_completion_document_missing')
    posted = json.loads(document['posted_manifest_json'])
    plan_manifest={k:posted[k] for k in ('contract_name','request_id','document_kind','business_date','source','feature_epoch','primary_document_id','root_document_id','documents','domain')}
    domain = posted['domain']; source = json.loads(row['source_json'])
    if (_fingerprint(posted) != document['posted_manifest_sha256']
            or request['posted_manifest_sha256'] != _fingerprint(plan_manifest)
            or posted['request_id'] != identity or posted['business_date'] != request['business_date']
            or posted['document_id'] != document['document_id']
            or row['actor'] != request['actor'] or document['actor'] != request['actor']
            or Decimal(domain['amount_rub']) != Decimal(source['manifest']['amount_rub'])
            or domain['facility_id'] != source['manifest']['facility_id'] or domain['scope'] != source['manifest']['scope']):
        raise ValueError('overhead_completion_posted_source_changed')
    lines = posted['lines']; nm_ids = sorted({int(l['nm_id']) for l in lines})
    pools = sorted({str(l['pool']) for l in lines})
    revision = _fingerprint(dict(contract='pool_overhead_targeted_publication_v1',document_id=document['document_id'],
        request_source_revision=request['source_revision'],facility_id=domain['facility_id'],pools=pools,nm_ids=nm_ids,
        basis_digest=domain['basis_digest'],amount_rub=domain['amount_rub'],posted_manifest_sha256=_fingerprint(plan_manifest)))
    stable = 'pool_overhead:' + document['document_id']
    queue_id = 'whrq_' + hashlib.sha256(_json(dict(stable_source_id=stable,source_revision=revision)).encode()).hexdigest()[:24]
    queue = conn.execute(f'SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} WHERE queue_id=?', (queue_id,)).fetchone()
    source_queues=conn.execute(f'SELECT queue_id FROM {TARGETED_RECALC_QUEUE_TABLE} WHERE stable_source_id=?',(stable,)).fetchall()
    if (len(source_queues)!=1 or queue is None or queue['status'] != 'complete' or queue['stable_source_id'] != stable
            or queue['source_revision'] != revision or queue['effective_date'] != request['business_date']
            or json.loads(queue['affected_nm_ids_json']) != nm_ids):
        raise ValueError('overhead_completion_queue_identity_changed')
    # Bind native material rows too; immutability is not replaced by a public flag.
    materials = {}
    for table in ('sheet_vitrina_v1_ff_pool_document_lines','sheet_vitrina_v1_ff_pool_document_expense_lines'):
        materials[table] = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE document_id=? ORDER BY rowid', (document['document_id'],))]
    if sum(Decimal(r['amount_rub']) for r in materials['sheet_vitrina_v1_ff_pool_document_expense_lines']) != Decimal(domain['amount_rub']):
        raise ValueError('overhead_completion_money_changed')
    recovery = conn.execute('SELECT * FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?', (request['recovery_operation_id'],)).fetchone()
    scope = json.loads(recovery['target_scope_json']) if recovery else {}
    if (not recovery or recovery['tier'] != 'T1' or recovery['operation_kind'] != 'ff_pool_document_posting'
            or recovery['source_digest'] != request['request_identity'] or scope.get('request_id') != identity
            or scope.get('document_kind') != 'pool_overhead' or recovery['lifecycle_state'] not in ('mutation_running','retained')
            or not str(recovery['checkpoint_digest']).startswith('sha256:')):
        raise ValueError('overhead_completion_recovery_unbound')
    from packages.application.fbs_snapshot_cost_sources import _documents
    native=next((d for d in _documents(conn, document_id=document['document_id']) if d['document_id']==document['document_id']),None)
    if native is None: raise ValueError('overhead_completion_native_document_missing')
    return dict(native_document_fingerprint=native['fingerprint'],request_id=identity,source_digest=row['source_digest'],source_json=row['source_json'],actor=row['actor'],
        posted_manifest=posted,materials=materials,recovery_operation_id=request['recovery_operation_id'],
        queue_id=queue_id,stable_source_id=stable,source_revision=revision,effective_date=queue['effective_date'],affected_nm_ids=nm_ids)


def requires_current_completion(db_path, identity):
    """A recorded cycle proof cannot be bypassed by a legacy resume call."""
    with closing(readonly(db_path)) as conn:
        row=conn.execute(f'SELECT receipt_json FROM {TABLE} WHERE request_id=?',(identity,)).fetchone() if _exists(conn) else None
        return bool(row and json.loads(row[0]).get('native_completion'))


def capture_pending_completion(runtime, *, finance_block=None):
    """Capture existing complete queue tails before this pass's native Finance."""
    from packages.application.storage_registry import StoreRegistry
    registry = StoreRegistry(runtime.runtime_dir); authority = registry.load()
    if registry.resolve('operational',manifest=authority) != Path(runtime.db_path).resolve():
        raise ValueError('overhead_completion_storage_changed')
    with closing(readonly(runtime.db_path)) as conn:
        rows = conn.execute(f"SELECT request_id FROM {TABLE} WHERE state IN ('accepted','processing','delayed') ORDER BY accepted_at,request_id LIMIT 100").fetchall() if _exists(conn) else []
        sources = []
        for row in rows:
            # Unposted or still queued sources belong to another bounded pass.
            from packages.application.ff_pool_documents import REQUESTS_TABLE, TARGETED_RECALC_QUEUE_TABLE
            posted = conn.execute(f'SELECT posted_document_id FROM {REQUESTS_TABLE} WHERE request_id=?', (row[0],)).fetchone()
            queue = conn.execute(f"SELECT status FROM {TARGETED_RECALC_QUEUE_TABLE} WHERE stable_source_id=?", ('pool_overhead:'+posted[0],)).fetchone() if posted and posted[0] else None
            if queue and queue[0] == 'complete': sources.append(_completion_source(conn,row[0]))
    planned_finance=None
    if sources:
        from datetime import date
        if finance_block is None: raise ValueError('overhead_completion_finance_owner_required')
        finance_block._pin_active_cost()
        planned_finance=finance_block.plan_stale_cost_weeks(date_from=date.fromisoformat(finance_block.shared_cost_snapshot.effective_date))['fingerprint']
    if registry.load() != authority: raise ValueError('overhead_completion_storage_changed')
    return dict(authority=authority,sources=sources,finance_plan_fingerprint=planned_finance)


def _completion_split_tokens(observer, identity):
    """Live attached raw fences while the guarded main writer excludes main commits."""
    return {name:int(observer.execute('PRAGMA "'+name.replace('"','""')+'".data_version').fetchone()[0])
            for name,_ in identity if name!='main'}


class _CompletionReadset:
    """Exact native SELECTs for this cohort; stream their bytes without rebuilding Finance."""
    def __init__(self, connection):
        self.connection=connection
        self.queries={}

    def execute(self, sql, parameters=()):
        cursor=self.connection.execute(sql,parameters)
        if sql.lstrip().upper().startswith(('SELECT','PRAGMA')):
            self.queries[(sql,tuple(parameters))]=None
        return cursor

    def seal(self):
        import hashlib
        digest=hashlib.sha256()
        rows=0;size=0
        for sql,parameters in self.queries:
            cursor=self.connection.execute(sql,parameters)
            encoded=json.dumps([sql,parameters,cursor.description],default=lambda value:{'sqlite_blob_hex':value.hex()},separators=(',',':')).encode()
            digest.update(encoded);size+=len(encoded)
            for row in cursor:
                encoded=json.dumps(list(row),ensure_ascii=False,default=lambda value:{'sqlite_blob_hex':value.hex()},separators=(',',':')).encode()+b'\n'
                digest.update(encoded);size+=len(encoded);rows+=1
        return dict(digest='sha256:'+digest.hexdigest(),rows=rows,bytes=size,queries=len(self.queries))


def complete_current_cycle(runtime, captured, *, finance_block, finance_receipt, economics_receipt, now=None):
    """Oldest first, bounded coherent groups, without interrupting a begun finalizer."""
    from time import monotonic
    deadline=monotonic()+30
    completed=0
    sources=captured['sources']
    for offset in range(0,len(sources),8):
        if completed and monotonic()>=deadline:break
        cohort=sources[offset:offset+8]
        result=_complete_cohort(runtime,{**captured,'sources':cohort},finance_block=finance_block,
            finance_receipt=finance_receipt,economics_receipt=economics_receipt,now=now,deadline=deadline)
        completed+=result['processed_count']
        if result['processed_count']!=len(cohort):break
    return dict(processed_count=completed,pending_count=len(sources)-completed)


def _complete_cohort(runtime, captured, *, finance_block, finance_receipt, economics_receipt, now=None, deadline=None):
    """One coherent bounded cohort proof, then short guarded native handoffs."""
    from contextlib import ExitStack
    from datetime import date
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.fbs_overhead_presentation import OverheadAccountingView
    from packages.application.ff_pool_documents import TARGETED_RECALC_QUEUE_TABLE
    from packages.application.wb_finance_weekly import _nomenclature_identity_index, _resolve_finance_nm_id, _operation_date
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    require_warehouse_job_owner(runtime.runtime_dir)
    if not captured['sources']: return dict(processed_count=0)
    if (finance_receipt.get('fingerprint') != captured['finance_plan_fingerprint']
            or finance_receipt.get('status') not in ('applied','already_current')
            or finance_receipt.get('non_target_preserved') is not True
            or type(finance_receipt.get('post_verify_stale_week_count')) is not int
            or finance_receipt['post_verify_stale_week_count'] != 0
            or finance_receipt.get('source_advanced_after_apply') is True):
        raise ValueError('overhead_completion_finance_unproven')
    completed=0
    with ExitStack() as scope:
        authority=[]
        observer=scope.enter_context(closing(finance_block._connect_stale_cost_plan(storage_authority=authority)))
        registry=finance_block.store_registry
        if authority != [captured['authority']] or registry.load()!=captured['authority']:
            raise ValueError('overhead_completion_storage_changed')
        identity=finance_block._sqlite_persistent_identity(observer)
        def files_identity(connection):
            return tuple((name,path,Path(path).stat().st_dev,Path(path).stat().st_ino) for name,path in finance_block._sqlite_persistent_identity(connection))
        files=files_identity(observer)
        # BEFORE every scope/source read, on this same live, non-transactional observer.
        before=finance_block._sqlite_data_version_token(observer)
        book_observer=scope.enter_context(closing(sqlite3.connect(accounting.path(runtime.runtime_dir).as_uri()+'?mode=ro',uri=True)))
        book_observer.execute('PRAGMA query_only=ON')
        book_before=finance_block._sqlite_data_version_token(book_observer)
        book_identity=finance_block._sqlite_persistent_identity(book_observer)
        book_files=files_identity(book_observer)
        plan=finance_block._plan_stale_cost_weeks_in_connection(observer,
            date_from=date.fromisoformat(finance_block.shared_cost_snapshot.effective_date),date_to=None)
        if plan['stale_week_count']!=0: raise ValueError('overhead_completion_finance_stale')
        reads=_CompletionReadset(observer)
        aliases,ambiguous,_,_=_nomenclature_identity_index(reads)
        nm_ids=sorted({str(nm) for frozen in captured['sources'] for nm in frozen['affected_nm_ids']})
        earliest=min(frozen['effective_date'] for frozen in captured['sources'])
        # Exclude only canonical ASCII positive decimal IDs outside the source
        # cohort. Native Python str(...).strip() also accepts Unicode whitespace;
        # SQLite trim/casts must never decide that such rows are irrelevant.
        # Noncanonical, absent and zero forms are a conservative superset,
        # resolved below by the existing native resolver (including aliases).
        # Whole selected weeks retain every native aggregate operand.
        direct_nm="coalesce(CAST(json_extract(raw_json,'$.nmId') AS TEXT),'')"
        canonical_nm=f"({direct_nm} GLOB '[1-9]*' AND {direct_nm} NOT GLOB '*[^0-9]*')"
        candidates=reads.execute("SELECT week_start,week_end,raw_json FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_end>=? AND ("+direct_nm+" IN ("+
            ','.join('?' for _ in nm_ids)+") OR NOT "+canonical_nm+") ORDER BY week_start,report_id,rrd_id",
            (finance_block.seller_id,earliest,*nm_ids))
        keys=set()
        for row in candidates:
            operation=json.loads(row['raw_json'])
            nm,_,problem=_resolve_finance_nm_id(operation,alias_to_nm=aliases,ambiguous_aliases=ambiguous)
            if problem:
                from decimal import Decimal
                if str(operation.get('docTypeName') or '').casefold() in {'продажа','возврат'} and Decimal(str(operation.get('quantity') or 0))!=0:
                    raise ValueError('overhead_completion_finance_scope_unknown')
                continue
            day,day_source=_operation_date(operation,date.fromisoformat(row['week_start']))
            for frozen in captured['sources']:
                if str(nm) not in {str(n) for n in frozen['affected_nm_ids']}: continue
                if day_source=='week_start_fallback':raise ValueError('overhead_completion_finance_operation_date_unknown')
                if day.isoformat()>=frozen['effective_date']:
                    keys.add((finance_block.seller_id,str(row['week_start']),str(row['week_end'])))
        image=dict(source=finance_block._finance_source_dependency_fingerprint(reads,target_keys=keys,force_reload=True),
            target_digest=finance_block._json_digest(finance_block._finance_target_images(reads,keys)))
        if finance_receipt['status']=='already_current':
            if finance_receipt.get('fingerprint')!=plan['fingerprint'] or finance_receipt.get('weeks')!=[]:
                raise ValueError('overhead_completion_finance_foreign')
        else:
            targets={(finance_block.seller_id,str(w['week_start']),str(w['week_end'])) for w in finance_receipt.get('weeks',[])}
            actual=finance_block._finance_source_dependency_fingerprint(observer,target_keys=targets,force_reload=True)
            if (not targets or finance_receipt.get('source_dependency')!=actual
                    or finance_receipt.get('post_source_dependency')!=actual
                    or finance_receipt.get('target_image_digest')!=finance_block._json_digest(finance_block._finance_target_images(observer,targets))):
                raise ValueError('overhead_completion_finance_foreign')
        publication=accounting.current_publication_receipt(runtime,now=now)
        if (not publication or economics_receipt.get('accounting_publication')!=publication
                or finance_receipt.get('accounting_version')!=publication['accounting_version']
                or finance_receipt.get('accounting_version_before')!=publication['accounting_version']
                or finance_receipt.get('accounting_version_unchanged') is not True):
            raise ValueError('overhead_completion_economics_foreign')
        # Seal Ready's actual bytes without re-parsing/re-rendering all cells at each handoff.
        reads.execute('SELECT p.operation_id,p.attempt_id,p.after_digest,p.book_version,p.state,p.ready_required,ready.plan_json,current.bundle_version FROM sheet_vitrina_v1_ready_publications p JOIN sheet_vitrina_v1_ready_snapshots ready ON ready.bundle_version=p.bundle_version AND ready.as_of_date=p.as_of_date JOIN registry_upload_current_state current ON current.bundle_version=ready.bundle_version AND current.slot=1 WHERE p.operation_id=? AND p.attempt_id=?',
            (publication['operation_id'],publication['attempt_id'])).fetchall()
        book,version=accounting.load(runtime.runtime_dir)
        allocations={}
        with closing(readonly(runtime.db_path)) as conn:
            for frozen in captured['sources']:
                if _completion_source(conn,frozen['request_id'])!=frozen:raise ValueError('overhead_completion_source_changed')
                acceptance=_public(conn,conn.execute(f'SELECT * FROM {TABLE} WHERE request_id=?',(frozen['request_id'],)).fetchone())
                allocation=OverheadAccountingView(runtime.runtime_dir,runtime.db_path,now=now).resolve(
                    acceptance['summary'],day=acceptance['business_date'],document_id=acceptance['document']['document_id'],posted=True)
                if (version!=publication['accounting_version'] or book['state']['periods'].get(frozen['effective_date'],{}).get('applied_documents',{}).get(frozen['posted_manifest']['document_id'])!=frozen['native_document_fingerprint']):
                    raise ValueError('overhead_completion_dated_document_changed')
                if not allocation or allocation['allocation_status']!='published' or allocation['accounting_version']!=version:
                    raise ValueError('overhead_completion_allocation_unproven')
                allocations[frozen['request_id']]=allocation
        seal=reads.seal()
        if (observer.in_transaction or before!=finance_block._sqlite_data_version_token(observer)
                or book_before!=finance_block._sqlite_data_version_token(book_observer)):
            raise ValueError('overhead_completion_readback_changed')
        fence=before
        def refresh_fence():
            nonlocal fence
            # Own native commits change main.data_version. Never reset the fence
            # until actual retained source/target/scope bytes have been streamed.
            start=finance_block._sqlite_data_version_token(observer)
            if (registry.load()!=captured['authority'] or finance_block._sqlite_persistent_identity(observer)!=identity
                    or files_identity(observer)!=files
                    or finance_block._sqlite_persistent_identity(book_observer)!=book_identity
                    or files_identity(book_observer)!=book_files
                    or finance_block._sqlite_data_version_token(book_observer)!=book_before
                    or reads.seal()!=seal):
                raise ValueError('overhead_completion_cohort_changed')
            if start!=finance_block._sqlite_data_version_token(observer) or registry.load()!=captured['authority']:
                raise ValueError('overhead_completion_readback_changed')
            fence=start
        for frozen in captured['sources']:
            from time import monotonic
            if completed and deadline is not None and monotonic()>=deadline:break
            transactions={}
            def guard(conn=None, *, begin=False, initial_changes=None, transaction=None):
                if conn is None:
                    refresh_fence()
                    with closing(readonly(runtime.db_path)) as opened: return guard(opened)
                writer=bool(conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0]!=1)
                if begin:
                    if not writer or initial_changes!=conn.total_changes:
                        raise ValueError('overhead_completion_pre_dml_fence_required')
                    transaction=object()
                    transactions[conn]=transaction
                elif writer and (transaction is None or transactions.get(conn) is not transaction):
                    raise ValueError('overhead_completion_transaction_unbound')
                # Only a caller-bound BEGIN may avoid the main observer after own DML.
                # Rollback-mode blob row updates can hold EXCLUSIVE cache-spill locks.
                post_dml=bool(writer and not begin)
                def token():
                    return (_completion_split_tokens(observer,identity) if post_dml
                            else finance_block._sqlite_data_version_token(observer))
                expected={k:v for k,v in fence.items() if not post_dml or k!='main'}
                if (registry.load()!=captured['authority']
                        or Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()!=registry.resolve('operational',manifest=captured['authority'])
                        or finance_block._sqlite_persistent_identity(observer)!=identity
                        or token()!=expected or files_identity(observer)!=files
                        or finance_block._sqlite_persistent_identity(book_observer)!=book_identity
                        or files_identity(book_observer)!=book_files
                        or finance_block._sqlite_data_version_token(book_observer)!=book_before):
                    raise ValueError('overhead_completion_authority_or_readback_changed')
                if _completion_source(conn,frozen['request_id'])!=frozen:
                    raise ValueError('overhead_completion_source_changed')
                if token()!=expected or registry.load()!=captured['authority']:
                    raise ValueError('overhead_completion_readback_changed')
                return transaction
            def prepare_guard():
                guard() # Expensive streaming outside any operational writer.
                from packages.application.storage_registry import manifest_payload
                proof=dict(contract='confirmed_overhead_native_completion_v2',source=frozen,allocation=allocations[frozen['request_id']],
                    storage_authority={**manifest_payload(captured['authority']),'implicit':captured['authority'].implicit},
                    storage_manifest_sha256=captured['authority'].manifest_sha256,accounting_publication=publication,economics_publication=economics_receipt,
                    finance_publication=dict(receipt=finance_receipt,zero_stale_plan_fingerprint=plan['fingerprint'],applicable_weeks=sorted(keys),cohort_seal=seal,**image))
                with closing(registry.connect('operational',mode='rw',operation='operator_overhead_completion',manifest=captured['authority'])) as writer:
                    initial_changes=writer.total_changes
                    writer.execute('BEGIN IMMEDIATE')
                    try:
                        transaction=guard(writer,begin=True,initial_changes=initial_changes)
                        changed=writer.execute(f"UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET economics_status='complete',economics_finished_at=?,economics_error='',finance_status='complete',finance_finished_at=?,finance_error='',finance_source_fingerprint=? WHERE queue_id=? AND stable_source_id=? AND source_revision=? AND effective_date=? AND affected_nm_ids_json=? AND status='complete'",
                            (datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),finance_receipt['fingerprint'],frozen['queue_id'],frozen['stable_source_id'],frozen['source_revision'],frozen['effective_date'],json.dumps(frozen['affected_nm_ids'],separators=(',',':')))).rowcount
                        if changed!=1:raise ValueError('overhead_completion_queue_cas_failed')
                        writer.execute(f'UPDATE {TABLE} SET receipt_json=? WHERE request_id=? AND source_digest=?',
                            (json.dumps(dict(native_completion=proof),ensure_ascii=False),frozen['request_id'],frozen['source_digest']))
                        guard(writer,transaction=transaction);writer.commit()
                    except BaseException:writer.rollback();raise
                return guard
            with closing(readonly(runtime.db_path)) as conn:
                acceptance=_public(conn,conn.execute(f'SELECT * FROM {TABLE} WHERE request_id=?',(frozen['request_id'],)).fetchone())
            def finish(recovery,guard):
                with closing(readonly(runtime.db_path)) as conn:
                    receipt=json.loads(conn.execute(f'SELECT receipt_json FROM {TABLE} WHERE request_id=?',(frozen['request_id'],)).fetchone()[0])
                receipt.update(native_state='complete',native_recovery=recovery,native_posted=True,document_id=acceptance['document']['document_id'])
                _update(runtime.db_path,frozen['request_id'],'completed','',receipt,guard=guard)
            _finalize_confirmed(runtime,acceptance,prepare_guard=prepare_guard,finish=finish)
            completed+=1
    return dict(processed_count=completed)
