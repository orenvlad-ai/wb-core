"""Confirmed FF expense receipts and their bounded, query-only operation journal.

Preview requests remain previews. A confirmation owns a frozen source receipt;
posting and dated accounting publication remain separately evidenced effects.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3

TABLE = "sheet_vitrina_v1_ff_pool_overhead_confirmations"
PREFIX = "/v1/sheet-vitrina-v1/operations"
NATIVE = "/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/requests/"
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


def _update(db_path, identity, state, reason, receipt=None):
    with sqlite3.connect(db_path, timeout=2) as conn:
        conn.execute(f"UPDATE {TABLE} SET state=?,reason_code=?,updated_at=?,receipt_json=? WHERE request_id=?",
                     (state, reason, datetime.now(timezone.utc).isoformat(), json.dumps(receipt or {}, ensure_ascii=False), identity))


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


def _finalize_confirmed(runtime, acceptance):
    """Retain only this posted source, under the same owners as native posting."""
    from packages.application.ff_pool_documents import FfPoolDocumentService, REQUESTS_TABLE, _fingerprint
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    require_warehouse_job_owner(runtime.runtime_dir)
    identity = acceptance["request_id"]
    with (Path(runtime.runtime_dir) / ".ff-pool-document-posting.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5):
                with closing(readonly(runtime.db_path)) as conn:
                    request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (identity,)).fetchone()
                    assert_native_confirmation(conn, request)
                    if request["posted_document_id"] != acceptance["document"]["document_id"]:
                        raise ValueError("confirmed_overhead_document_changed")
                service = FfPoolDocumentService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
                                               resume=False, bootstrap=False)
                service._finalize_posted(identity)
                readback = service._verify_posted_readback(identity)
                with closing(readonly(runtime.db_path)) as conn:
                    row = conn.execute(f"""SELECT r.state,recovery.operation_id,recovery.lifecycle_state,recovery.after_digest
                        FROM {REQUESTS_TABLE} r LEFT JOIN sheet_vitrina_v1_recovery_operations recovery
                        ON recovery.operation_id=r.recovery_operation_id WHERE r.request_id=?""", (identity,)).fetchone()
                    if (row["state"] != "complete" or row["lifecycle_state"] != "retained"
                            or row["after_digest"] != _fingerprint(readback)):
                        raise ValueError("confirmed_overhead_native_not_terminal")
                    return {"operation_id": row["operation_id"], "lifecycle": row["lifecycle_state"],
                            "after_digest": row["after_digest"]}
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
