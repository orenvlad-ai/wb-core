"""Durable, recoverable operator workflows for FF business actions.

The FF ledger and warehouse functional read model remain the canonical business
stores.  This module only owns request/job state for previews and derives the
operator state machine from durable preview, document, queue and economics
journal rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Iterable, Mapping

from packages.application.ff_inventory_reconciliation import (
    FfInventoryReconciliation,
    FfInventoryReconciliationError,
    _parse_target_workbook,
    ensure_inventory_reconciliation_schema,
)
from packages.application.ff_overhead_allocation import (
    FfOverheadAllocation,
    FfOverheadAllocationError,
    ensure_ff_overhead_schema,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sqlite_contention import sqlite_operation_context
from packages.application.warehouse_functional import ensure_warehouse_functional_schema


INVENTORY_ACTION = "inventory"
OVERHEAD_ACTION = "overhead"
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}")
INVENTORY_TABLE = "sheet_vitrina_v1_ff_inventory_previews"
OVERHEAD_TABLE = "sheet_vitrina_v1_ff_overhead_previews"
QUEUE_TABLE = "sheet_vitrina_v1_warehouse_targeted_recalc_queue"
EVENT_TABLE = "sheet_vitrina_v1_ff_workflow_events"
ALIAS_TABLE = "sheet_vitrina_v1_ff_workflow_request_aliases"


class FfDocumentWorkflow:
    """Accept fast, process asynchronously and expose exact durable readback."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        inventory: FfInventoryReconciliation,
        overhead: FfOverheadAllocation,
        timestamp_factory: Any,
        start_workers: bool = True,
    ) -> None:
        self.runtime = runtime
        self.inventory = inventory
        self.overhead = overhead
        self.timestamp_factory = timestamp_factory
        self.start_workers = bool(start_workers)
        self._worker_lock = threading.Lock()
        self._inflight: set[tuple[str, str]] = set()
        with _connect(self.runtime.db_path) as conn:
            ensure_ff_document_workflow_schema(conn)
            # A process restart cannot retain an in-process planner.  Returning
            # these jobs to accepted is safe because the exact identity is
            # unique and preview planning itself is query-only.
            conn.execute(
                f"UPDATE {INVENTORY_TABLE} SET status='accepted',updated_at=? "
                "WHERE status='processing'",
                (self._now(),),
            )
            conn.execute(
                f"UPDATE {OVERHEAD_TABLE} SET status='accepted',updated_at=? "
                "WHERE status='processing'",
                (self._now(),),
            )
            conn.commit()
        if self.start_workers:
            self.resume_incomplete()

    def accept_inventory(
        self,
        *,
        source_bytes: bytes,
        source_filename: str,
        business_date: str,
        request_id: str,
        actor: str,
        return_supply_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        started = time.monotonic()
        request_key = _request_id(request_id)
        normalized_returns = sorted(
            {str(item).strip() for item in return_supply_ids if str(item).strip()}
        )
        source_sha256 = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
        identity = "sha256:" + _digest(
            {
                "source_sha256": source_sha256,
                "business_date": str(business_date),
                "return_supply_ids": normalized_returns,
            }
        )
        now = self._now()
        preview_id = "ffip_" + identity.removeprefix("sha256:")[:24]
        with _connect(self.runtime.db_path) as conn:
            ensure_ff_document_workflow_schema(conn)
            existing = conn.execute(
                f"SELECT * FROM {INVENTORY_TABLE} WHERE request_identity=? "
                "ORDER BY created_at DESC,preview_id DESC LIMIT 1",
                (identity,),
            ).fetchone()
            if existing is None:
                existing = next(
                    (
                        row
                        for row in conn.execute(
                            f"SELECT * FROM {INVENTORY_TABLE} "
                            "WHERE source_sha256=? AND business_date=? AND request_identity='' "
                            "ORDER BY created_at DESC,preview_id DESC",
                            (source_sha256, str(business_date)),
                        ).fetchall()
                        if sorted(_loads(row["return_supply_ids_json"], []))
                        == normalized_returns
                    ),
                    None,
                )
            if existing is not None:
                self._assert_request_identity(
                    conn,
                    table=INVENTORY_TABLE,
                    request_id=request_key,
                    identity=identity,
                )
                if not str(existing["request_id"] or ""):
                    conn.execute(
                        f"UPDATE {INVENTORY_TABLE} SET request_id=?,request_identity=?,updated_at=? "
                        "WHERE preview_id=?",
                        (request_key, identity, now, str(existing["preview_id"])),
                    )
                self._remember_request_alias(
                    conn,
                    action_type=INVENTORY_ACTION,
                    request_id=request_key,
                    preview_id=str(existing["preview_id"]),
                    identity=identity,
                    accepted_at=now,
                )
                conn.commit()
                status = self.inventory_status(preview_id=str(existing["preview_id"]))
                if str(status.get("state")) in {"accepted", "processing"}:
                    self._dispatch(INVENTORY_ACTION, str(existing["preview_id"]))
                return {
                    **status,
                    "idempotent": True,
                    "http_status": 422 if status.get("state") in {"blocked", "error"} else 202,
                }

            validation_error: FfInventoryReconciliationError | None = None
            try:
                # Workbook structure/date validation is bounded and DB-free.
                # Heavy SKU/cost planning is never part of the HTTP request.
                _parse_target_workbook(source_bytes, business_date=str(business_date))
            except FfInventoryReconciliationError as exc:
                validation_error = exc
            initial_status = "blocked" if validation_error is not None else "accepted"
            error_code = validation_error.code if validation_error is not None else ""
            error_details = validation_error.details if validation_error is not None else None
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_request_identity(
                    conn,
                    table=INVENTORY_TABLE,
                    request_id=request_key,
                    identity=identity,
                )
                inserted = conn.execute(
                    f"""
                    INSERT INTO {INVENTORY_TABLE}(
                        preview_id,source_sha256,source_filename,source_file_blob,
                        business_date,return_supply_ids_json,plan_fingerprint,
                        plan_json,created_at,status,request_id,request_identity,
                        requested_by,accepted_at,started_at,finished_at,updated_at,
                        error_code,error_details_json,reconciliation_id
                    ) VALUES(?,?,?,?,?,?,'','{{}}',?,?,?,?,?,?,'',?,?,?,?,'')
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        preview_id,
                        source_sha256,
                        str(source_filename or "inventory.xlsx"),
                        sqlite3.Binary(source_bytes),
                        str(business_date),
                        _json(normalized_returns),
                        now,
                        initial_status,
                        request_key,
                        identity,
                        str(actor or "operator"),
                        now,
                        now if validation_error is not None else "",
                        now,
                        error_code,
                        _json(error_details),
                    ),
                ).rowcount
                self._remember_request_alias(
                    conn,
                    action_type=INVENTORY_ACTION,
                    request_id=request_key,
                    preview_id=preview_id,
                    identity=identity,
                    accepted_at=now,
                )
                acceptance_ms = max(0, int((time.monotonic() - started) * 1000))
                if inserted:
                    self._event(
                        conn,
                        action_type=INVENTORY_ACTION,
                        identity=preview_id,
                        stage="file_accepted",
                        status="complete",
                        occurred_at=now,
                        duration_ms=acceptance_ms,
                    )
                    if validation_error is not None:
                        self._event(
                            conn,
                            action_type=INVENTORY_ACTION,
                            identity=preview_id,
                            stage="validation",
                            status="blocked",
                            occurred_at=now,
                            details={"code": error_code},
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if validation_error is None:
            self._dispatch(INVENTORY_ACTION, preview_id)
        if inserted:
            _emit_metric(
                action_type=INVENTORY_ACTION,
                identity=preview_id,
                stage="file_accepted",
                status="complete",
                duration_ms=acceptance_ms,
            )
        status = self.inventory_status(preview_id=preview_id)
        return {
            **status,
            "idempotent": not bool(inserted),
            "http_status": 422 if validation_error is not None else 202,
        }

    def accept_overhead(
        self,
        *,
        business_date: str,
        amount_rub: Any,
        reason: str,
        request_id: str,
        actor: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        request_key = _request_id(request_id)
        input_payload = {
            "business_date": str(business_date or "").strip(),
            "amount_rub": str(amount_rub if amount_rub is not None else "").strip(),
            "reason": " ".join(str(reason or "").split()),
        }
        identity = "sha256:" + _digest(input_payload)
        preview_id = "ffop_" + identity.removeprefix("sha256:")[:24]
        now = self._now()
        with _connect(self.runtime.db_path) as conn:
            ensure_ff_document_workflow_schema(conn)
            existing = conn.execute(
                f"SELECT * FROM {OVERHEAD_TABLE} WHERE request_identity=? "
                "ORDER BY created_at DESC,preview_id DESC LIMIT 1",
                (identity,),
            ).fetchone()
            if existing is not None:
                self._assert_request_identity(
                    conn,
                    table=OVERHEAD_TABLE,
                    request_id=request_key,
                    identity=identity,
                )
                self._remember_request_alias(
                    conn,
                    action_type=OVERHEAD_ACTION,
                    request_id=request_key,
                    preview_id=str(existing["preview_id"]),
                    identity=identity,
                    accepted_at=now,
                )
                conn.commit()
                status = self.overhead_status(preview_id=str(existing["preview_id"]))
                if str(status.get("state")) in {"accepted", "processing"}:
                    self._dispatch(OVERHEAD_ACTION, str(existing["preview_id"]))
                return {**status, "idempotent": True, "http_status": 202}
            self._assert_request_identity(
                conn,
                table=OVERHEAD_TABLE,
                request_id=request_key,
                identity=identity,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {OVERHEAD_TABLE}(
                    preview_id,request_id,request_identity,business_date,amount_rub,
                    reason,plan_fingerprint,plan_json,document_id,created_by,
                    created_at,accepted_at,started_at,finished_at,updated_at,status,
                    error_code,error_details_json
                ) VALUES(?,?,?,?,?,?,'','{{}}','',?,?,?,'','',?,'accepted','','null')
                ON CONFLICT DO NOTHING
                """,
                (
                    preview_id,
                    request_key,
                    identity,
                    input_payload["business_date"],
                    input_payload["amount_rub"],
                    input_payload["reason"],
                    str(actor or "operator"),
                    now,
                    now,
                    now,
                ),
            ).rowcount
            self._remember_request_alias(
                conn,
                action_type=OVERHEAD_ACTION,
                request_id=request_key,
                preview_id=preview_id,
                identity=identity,
                accepted_at=now,
            )
            acceptance_ms = max(0, int((time.monotonic() - started) * 1000))
            if inserted:
                self._event(
                    conn,
                    action_type=OVERHEAD_ACTION,
                    identity=preview_id,
                    stage="data_accepted",
                    status="complete",
                    occurred_at=now,
                    duration_ms=acceptance_ms,
                )
            conn.commit()
        self._dispatch(OVERHEAD_ACTION, preview_id)
        if inserted:
            _emit_metric(
                action_type=OVERHEAD_ACTION,
                identity=preview_id,
                stage="data_accepted",
                status="complete",
                duration_ms=acceptance_ms,
            )
        return {
            **self.overhead_status(preview_id=preview_id),
            "idempotent": not bool(inserted),
            "http_status": 202,
        }

    def inventory_status(
        self,
        *,
        preview_id: str = "",
        request_id: str = "",
        source_sha256: str = "",
        business_date: str = "",
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path, query_only=True) as conn:
            row = _select_inventory_preview(
                conn,
                preview_id=preview_id,
                request_id=request_id,
                source_sha256=source_sha256,
                business_date=business_date,
            )
            if row is None:
                return _not_found(INVENTORY_ACTION)
            payload = self._inventory_public(conn, row)
        if payload["state"] in {"accepted", "processing"}:
            self._dispatch(INVENTORY_ACTION, str(row["preview_id"]))
        return payload

    def overhead_status(
        self,
        *,
        preview_id: str = "",
        request_id: str = "",
        document_id: str = "",
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path, query_only=True) as conn:
            preview = _select_overhead_preview(
                conn,
                preview_id=preview_id,
                request_id=request_id,
                document_id=document_id,
            )
            preview_plan = dict(_loads(preview["plan_json"], {})) if preview is not None else {}
            planned_document_id = str(
                preview_plan.get("document_id")
                or dict(preview_plan.get("manifest") or {}).get("document_id")
                or ""
            )
            document = _select_overhead_document(
                conn,
                document_id=(
                    document_id
                    or (str(preview["document_id"]) if preview else "")
                    or planned_document_id
                ),
                latest=not any((preview_id, request_id, document_id)),
            )
            if (
                preview is not None
                and document is not None
                and not str(preview["document_id"] or "")
                and str(preview["created_at"] or "") >= str(document["created_at"] or "")
            ):
                document = None
            if preview is None and document is None:
                return _not_found(OVERHEAD_ACTION)
            payload = self._overhead_public(conn, preview, document)
        if preview is not None and payload["state"] in {"accepted", "processing"}:
            self._dispatch(OVERHEAD_ACTION, str(preview["preview_id"]))
        return payload

    def note_inventory_confirmed(
        self,
        *,
        preview_id: str,
        reconciliation_id: str,
        actor: str,
        duration_ms: int = 0,
    ) -> None:
        now = self._now()
        with _connect(self.runtime.db_path) as conn:
            changed = conn.execute(
                f"UPDATE {INVENTORY_TABLE} SET status='confirmed',reconciliation_id=?,"
                "requested_by=CASE WHEN requested_by='' THEN ? ELSE requested_by END,"
                "finished_at=?,updated_at=? WHERE preview_id=? "
                "AND (status<>'confirmed' OR reconciliation_id<>?)",
                (reconciliation_id, actor, now, now, preview_id, reconciliation_id),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    action_type=INVENTORY_ACTION,
                    identity=preview_id,
                    stage="document_committed",
                    status="complete",
                    occurred_at=now,
                    duration_ms=duration_ms,
                    details={"reconciliation_id": reconciliation_id},
                )
            conn.commit()
        if changed:
            _emit_metric(
                action_type=INVENTORY_ACTION,
                identity=preview_id,
                stage="document_committed",
                status="complete",
                duration_ms=duration_ms,
            )

    def note_overhead_confirmed(
        self,
        *,
        preview_id: str,
        document_id: str,
        actor: str,
        duration_ms: int = 0,
    ) -> None:
        if not preview_id:
            return
        now = self._now()
        with _connect(self.runtime.db_path) as conn:
            changed = conn.execute(
                f"UPDATE {OVERHEAD_TABLE} SET status='confirmed',document_id=?,"
                "created_by=CASE WHEN created_by='' THEN ? ELSE created_by END,"
                "finished_at=?,updated_at=? WHERE preview_id=? "
                "AND (status<>'confirmed' OR document_id<>?)",
                (document_id, actor, now, now, preview_id, document_id),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    action_type=OVERHEAD_ACTION,
                    identity=preview_id,
                    stage="document_committed",
                    status="complete",
                    occurred_at=now,
                    duration_ms=duration_ms,
                    details={"document_id": document_id},
                )
            conn.commit()
        if changed:
            _emit_metric(
                action_type=OVERHEAD_ACTION,
                identity=preview_id or document_id,
                stage="document_committed",
                status="complete",
                duration_ms=duration_ms,
            )

    def resume_incomplete(self) -> None:
        with _connect(self.runtime.db_path, query_only=True) as conn:
            inventory_ids = [
                str(row["preview_id"])
                for row in conn.execute(
                    f"SELECT preview_id FROM {INVENTORY_TABLE} WHERE status='accepted'"
                ).fetchall()
            ]
            overhead_ids = [
                str(row["preview_id"])
                for row in conn.execute(
                    f"SELECT preview_id FROM {OVERHEAD_TABLE} WHERE status='accepted'"
                ).fetchall()
            ]
        for preview_id in inventory_ids:
            self._dispatch(INVENTORY_ACTION, preview_id)
        for preview_id in overhead_ids:
            self._dispatch(OVERHEAD_ACTION, preview_id)

    def _dispatch(self, action_type: str, preview_id: str) -> None:
        if not self.start_workers or not preview_id:
            return
        key = (action_type, preview_id)
        with self._worker_lock:
            if key in self._inflight:
                return
            self._inflight.add(key)
        thread = threading.Thread(
            target=self._worker_entry,
            args=(action_type, preview_id),
            name=f"ff-{action_type}-preview-{preview_id[-6:]}",
            daemon=True,
        )
        thread.start()

    def _worker_entry(self, action_type: str, preview_id: str) -> None:
        try:
            with sqlite_operation_context(
                endpoint=f"ff/{action_type}/preview-worker",
                operation="PLAN",
                phase="async_preview",
                priority="background",
                owner="ff-document-preview-worker",
            ):
                if action_type == INVENTORY_ACTION:
                    self._process_inventory(preview_id)
                else:
                    self._process_overhead(preview_id)
        finally:
            with self._worker_lock:
                self._inflight.discard((action_type, preview_id))

    def _process_inventory(self, preview_id: str) -> None:
        started = time.monotonic()
        now = self._now()
        with _connect(self.runtime.db_path) as conn:
            claimed = conn.execute(
                f"UPDATE {INVENTORY_TABLE} SET status='processing',started_at=?,updated_at=?,"
                "error_code='',error_details_json='null' WHERE preview_id=? AND status='accepted'",
                (now, now, preview_id),
            ).rowcount
            if claimed != 1:
                return
            self._event(
                conn,
                action_type=INVENTORY_ACTION,
                identity=preview_id,
                stage="validation",
                status="running",
                occurred_at=now,
            )
            row = conn.execute(
                f"SELECT * FROM {INVENTORY_TABLE} WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            conn.commit()
        assert row is not None
        try:
            plan = self.inventory.build_plan(
                source_bytes=bytes(row["source_file_blob"]),
                source_filename=str(row["source_filename"]),
                business_date=str(row["business_date"]),
                return_supply_ids=_loads(row["return_supply_ids_json"], []),
            )
            final_status = "previewed" if bool(plan.get("apply_allowed")) else "blocked"
            error_code = "" if final_status == "previewed" else "plan_blocked"
            error_details = [] if final_status == "previewed" else list(plan.get("blockers") or [])
            self._finish_preview(
                action_type=INVENTORY_ACTION,
                preview_id=preview_id,
                table=INVENTORY_TABLE,
                status=final_status,
                fingerprint=str(plan.get("fingerprint") or ""),
                plan=plan,
                error_code=error_code,
                error_details=error_details,
                started=started,
            )
        except FfInventoryReconciliationError as exc:
            self._finish_preview(
                action_type=INVENTORY_ACTION,
                preview_id=preview_id,
                table=INVENTORY_TABLE,
                status="blocked",
                fingerprint="",
                plan={},
                error_code=exc.code,
                error_details=exc.details,
                started=started,
            )
        except Exception as exc:  # durable controlled worker failure
            self._finish_preview(
                action_type=INVENTORY_ACTION,
                preview_id=preview_id,
                table=INVENTORY_TABLE,
                status="failed",
                fingerprint="",
                plan={},
                error_code="preview_processing_failed",
                error_details={"error": str(exc).replace("\n", " ")[:1000]},
                started=started,
            )

    def _process_overhead(self, preview_id: str) -> None:
        started = time.monotonic()
        now = self._now()
        with _connect(self.runtime.db_path) as conn:
            claimed = conn.execute(
                f"UPDATE {OVERHEAD_TABLE} SET status='processing',started_at=?,updated_at=?,"
                "error_code='',error_details_json='null' WHERE preview_id=? AND status='accepted'",
                (now, now, preview_id),
            ).rowcount
            if claimed != 1:
                return
            self._event(
                conn,
                action_type=OVERHEAD_ACTION,
                identity=preview_id,
                stage="validation",
                status="running",
                occurred_at=now,
            )
            row = conn.execute(
                f"SELECT * FROM {OVERHEAD_TABLE} WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            conn.commit()
        assert row is not None
        try:
            plan = self.overhead.build_plan(
                business_date=str(row["business_date"]),
                amount_rub=str(row["amount_rub"]),
                reason=str(row["reason"]),
            )
            already_applied = bool(plan.get("idempotent")) and str(plan.get("document_id") or "")
            final_status = "confirmed" if already_applied else "previewed"
            self._finish_preview(
                action_type=OVERHEAD_ACTION,
                preview_id=preview_id,
                table=OVERHEAD_TABLE,
                status=final_status,
                fingerprint=str(plan.get("fingerprint") or ""),
                plan=plan,
                error_code="",
                error_details=None,
                started=started,
                document_id=str(plan.get("document_id") or ""),
            )
        except FfOverheadAllocationError as exc:
            self._finish_preview(
                action_type=OVERHEAD_ACTION,
                preview_id=preview_id,
                table=OVERHEAD_TABLE,
                status="blocked",
                fingerprint="",
                plan={},
                error_code=exc.code,
                error_details=exc.details,
                started=started,
            )
        except Exception as exc:  # durable controlled worker failure
            self._finish_preview(
                action_type=OVERHEAD_ACTION,
                preview_id=preview_id,
                table=OVERHEAD_TABLE,
                status="failed",
                fingerprint="",
                plan={},
                error_code="preview_processing_failed",
                error_details={"error": str(exc).replace("\n", " ")[:1000]},
                started=started,
            )

    def _finish_preview(
        self,
        *,
        action_type: str,
        preview_id: str,
        table: str,
        status: str,
        fingerprint: str,
        plan: Mapping[str, Any],
        error_code: str,
        error_details: Any,
        started: float,
        document_id: str = "",
    ) -> None:
        now = self._now()
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        extra = ",document_id=?" if table == OVERHEAD_TABLE else ""
        parameters: list[Any] = [
            status,
            fingerprint,
            _json(dict(plan)),
            now,
            now,
            error_code,
            _json(error_details),
        ]
        if table == OVERHEAD_TABLE:
            parameters.append(document_id)
        parameters.append(preview_id)
        with _connect(self.runtime.db_path) as conn:
            changed = conn.execute(
                f"UPDATE {table} SET status=?,plan_fingerprint=?,plan_json=?,"
                f"finished_at=?,updated_at=?,error_code=?,error_details_json=?{extra} "
                "WHERE preview_id=? AND status='processing'",
                tuple(parameters),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    action_type=action_type,
                    identity=preview_id,
                    stage="validation",
                    status="complete" if status in {"previewed", "confirmed"} else status,
                    occurred_at=now,
                    duration_ms=duration_ms,
                    details={"error_code": error_code} if error_code else {},
                )
            conn.commit()
        if changed:
            _emit_metric(
                action_type=action_type,
                identity=preview_id,
                stage="validation",
                status=status,
                duration_ms=duration_ms,
            )

    def _inventory_public(
        self,
        conn: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan = dict(_loads(row["plan_json"], {}))
        preview_manifest = dict(plan.get("manifest") or {})
        error_code = str(row["error_code"] or "")
        error_details = _loads(row["error_details_json"], None)
        reconciliation = _inventory_reconciliation(conn, row)
        applied_manifest = (
            dict(_loads(reconciliation["manifest_json"], {}))
            if reconciliation is not None
            else {}
        )
        manifest = applied_manifest or preview_manifest
        replay = _replay_state(
            conn,
            stable_source_id=(
                "ff_inventory:" + str(reconciliation["reconciliation_id"])
                if reconciliation is not None
                else ""
            ),
        )
        stored_status = str(row["status"] or "")
        if reconciliation is not None and str(reconciliation["status"] or "") == "applied":
            state = (
                "replay_complete"
                if replay["status"] == "complete"
                else "replay_error"
                if replay["status"] in {"error", "missing"}
                else "applied"
            )
        elif stored_status == "previewed" and bool(plan.get("apply_allowed")):
            state = "ready"
        elif stored_status in {"accepted", "processing"}:
            state = stored_status
        elif stored_status == "blocked":
            state = "blocked"
        else:
            state = "error"
        source = dict(preview_manifest.get("source") or manifest.get("source") or {})
        if not source:
            source = {
                "filename": str(row["source_filename"] or ""),
                "sha256": str(row["source_sha256"] or ""),
                "business_date": str(row["business_date"] or ""),
            }
        validation = _inventory_validation_public(
            code=error_code,
            details=error_details,
            business_date=str(row["business_date"] or ""),
        )
        return {
            "contract_name": "ff_document_workflow_v1",
            "action_type": INVENTORY_ACTION,
            "state": state,
            "preview_id": str(row["preview_id"]),
            "request_id": str(row["request_id"] or ""),
            "fingerprint": str(row["plan_fingerprint"] or ""),
            "source": source,
            "summary": _inventory_summary(manifest),
            "blockers": list(plan.get("blockers") or []),
            "validation": validation,
            "confirm_allowed": state == "ready",
            "document": _inventory_document_public(reconciliation),
            "replay": replay,
            "actor": str(
                (reconciliation["created_by"] if reconciliation is not None else "")
                or row["requested_by"]
                or ""
            ),
            "accepted_at": str(row["accepted_at"] or row["created_at"] or ""),
            "checked_at": str(row["finished_at"] or ""),
            "updated_at": str(row["updated_at"] or row["created_at"] or ""),
            "steps": _steps(state, replay),
        }

    def _overhead_public(
        self,
        conn: sqlite3.Connection,
        preview: Mapping[str, Any] | None,
        document: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        plan = dict(_loads(preview["plan_json"], {})) if preview is not None else {}
        manifest = dict(plan.get("manifest") or {})
        replay = _replay_state(
            conn,
            stable_source_id=(
                "ff_overhead:" + str(document["document_id"])
                if document is not None
                else ""
            ),
        )
        if document is not None and str(document["status"] or "") == "applied":
            state = (
                "replay_complete"
                if replay["status"] == "complete"
                else "replay_error"
                if replay["status"] in {"error", "missing"}
                else "applied"
            )
        elif preview is not None and str(preview["status"] or "") == "previewed":
            state = "ready"
        elif preview is not None and str(preview["status"] or "") in {"accepted", "processing"}:
            state = str(preview["status"])
        elif preview is not None and str(preview["status"] or "") == "blocked":
            state = "blocked"
        else:
            state = "error"
        details = _overhead_details(preview, manifest, document)
        error_code = str(preview["error_code"] or "") if preview is not None else ""
        error_details = _loads(preview["error_details_json"], None) if preview is not None else None
        return {
            "contract_name": "ff_document_workflow_v1",
            "action_type": OVERHEAD_ACTION,
            "state": state,
            "preview_id": str(preview["preview_id"] or "") if preview is not None else "",
            "request_id": str(preview["request_id"] or "") if preview is not None else "",
            "fingerprint": str(
                (preview["plan_fingerprint"] if preview is not None else "")
                or (document["plan_fingerprint"] if document is not None else "")
                or ""
            ),
            "confirm_allowed": state == "ready",
            "details": details,
            "document": _overhead_document_public(document),
            "replay": replay,
            "validation": {
                "code": error_code,
                "message_ru": _overhead_error_ru(error_code, error_details),
                "details": error_details,
            },
            "actor": str(
                (document["created_by"] if document is not None else "")
                or (preview["created_by"] if preview is not None else "")
                or ""
            ),
            "accepted_at": str(preview["accepted_at"] or "") if preview is not None else "",
            "checked_at": str(preview["finished_at"] or "") if preview is not None else "",
            "updated_at": str(
                (document["created_at"] if document is not None else "")
                or (preview["updated_at"] if preview is not None else "")
                or ""
            ),
            "steps": _steps(state, replay),
        }

    def _assert_request_identity(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        request_id: str,
        identity: str,
    ) -> None:
        row = conn.execute(
            f"SELECT request_identity FROM {table} WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is not None and str(row["request_identity"] or "") != identity:
            raise ValueError("request_id уже относится к другим данным; обновите форму и повторите")
        alias = conn.execute(
            f"SELECT request_identity FROM {ALIAS_TABLE} WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if alias is not None and str(alias["request_identity"] or "") != identity:
            raise ValueError("request_id уже относится к другим данным; обновите форму и повторите")

    def _remember_request_alias(
        self,
        conn: sqlite3.Connection,
        *,
        action_type: str,
        request_id: str,
        preview_id: str,
        identity: str,
        accepted_at: str,
    ) -> None:
        conn.execute(
            f"INSERT INTO {ALIAS_TABLE}(request_id,action_type,preview_id,request_identity,accepted_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
            (request_id, action_type, preview_id, identity, accepted_at),
        )

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        action_type: str,
        identity: str,
        stage: str,
        status: str,
        occurred_at: str,
        duration_ms: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        event_id = "ffwe_" + _digest(
            {
                "action_type": action_type,
                "identity": identity,
                "stage": stage,
                "status": status,
                "occurred_at": occurred_at,
            }
        )[:24]
        conn.execute(
            f"INSERT OR IGNORE INTO {EVENT_TABLE}(event_id,action_type,identity,stage,status,"
            "occurred_at,duration_ms,details_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                event_id,
                action_type,
                identity,
                stage,
                status,
                occurred_at,
                max(0, int(duration_ms)),
                _json(dict(details or {})),
            ),
        )

    def _now(self) -> str:
        return str(self.timestamp_factory())


def ensure_ff_document_workflow_schema(conn: sqlite3.Connection) -> None:
    ensure_inventory_reconciliation_schema(conn)
    ensure_ff_overhead_schema(conn)
    ensure_warehouse_functional_schema(conn)
    _ensure_columns(
        conn,
        INVENTORY_TABLE,
        {
            "request_id": "TEXT NOT NULL DEFAULT ''",
            "request_identity": "TEXT NOT NULL DEFAULT ''",
            "requested_by": "TEXT NOT NULL DEFAULT ''",
            "accepted_at": "TEXT NOT NULL DEFAULT ''",
            "started_at": "TEXT NOT NULL DEFAULT ''",
            "finished_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "error_code": "TEXT NOT NULL DEFAULT ''",
            "error_details_json": "TEXT NOT NULL DEFAULT 'null'",
            "reconciliation_id": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_columns(
        conn,
        QUEUE_TABLE,
        {
            "economics_status": "TEXT NOT NULL DEFAULT ''",
            "economics_started_at": "TEXT",
            "economics_finished_at": "TEXT",
            "economics_error": "TEXT",
        },
    )
    conn.executescript(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS ff_inventory_preview_request_id
        ON {INVENTORY_TABLE}(request_id) WHERE request_id<>'';
        CREATE INDEX IF NOT EXISTS ff_inventory_preview_source_date
        ON {INVENTORY_TABLE}(source_sha256,business_date,created_at DESC);
        CREATE TABLE IF NOT EXISTS {OVERHEAD_TABLE}(
            preview_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            request_identity TEXT NOT NULL UNIQUE,
            business_date TEXT NOT NULL,
            amount_rub TEXT NOT NULL,
            reason TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            document_id TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT NOT NULL,
            error_details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_overhead_preview_latest
        ON {OVERHEAD_TABLE}(created_at DESC,preview_id DESC);
        CREATE TABLE IF NOT EXISTS {EVENT_TABLE}(
            event_id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            identity TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_workflow_events_identity
        ON {EVENT_TABLE}(action_type,identity,occurred_at);
        CREATE TABLE IF NOT EXISTS {ALIAS_TABLE}(
            request_id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            preview_id TEXT NOT NULL,
            request_identity TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_workflow_request_alias_preview
        ON {ALIAS_TABLE}(action_type,preview_id,accepted_at);
        """
    )
    conn.execute(
        f"UPDATE {INVENTORY_TABLE} SET accepted_at=CASE WHEN accepted_at='' THEN created_at ELSE accepted_at END,"
        "updated_at=CASE WHEN updated_at='' THEN created_at ELSE updated_at END,"
        "finished_at=CASE WHEN finished_at='' AND status IN ('previewed','confirmed') THEN created_at ELSE finished_at END "
        "WHERE accepted_at='' OR updated_at='' OR "
        "(finished_at='' AND status IN ('previewed','confirmed'))"
    )


def mark_ff_replay_economics(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    queue_ids: Iterable[str],
    status: str,
    occurred_at: str,
    error: str = "",
) -> int:
    """Persist the economics half of exact targeted replay after its readback."""

    selected = sorted({str(item) for item in queue_ids if str(item)})
    if not selected:
        return 0
    placeholders = ",".join("?" for _ in selected)
    normalized = "complete" if status == "complete" else "error"
    with _connect(runtime.db_path) as conn:
        ensure_ff_document_workflow_schema(conn)
        rows = conn.execute(
            f"SELECT queue_id,stable_source_id,COALESCE(economics_started_at,finished_at,requested_at) AS stage_started_at "
            f"FROM {QUEUE_TABLE} WHERE queue_id IN ({placeholders}) "
            "AND (economics_status<>? OR economics_error<>?)",
            (*selected, normalized, str(error or "")[:1000]),
        ).fetchall()
        changed_ids = [str(row["queue_id"]) for row in rows]
        if not changed_ids:
            return 0
        changed_placeholders = ",".join("?" for _ in changed_ids)
        cursor = conn.execute(
            f"UPDATE {QUEUE_TABLE} SET economics_status=?,"
            "economics_started_at=COALESCE(economics_started_at,finished_at,?),"
            "economics_finished_at=?,economics_error=? "
            f"WHERE queue_id IN ({changed_placeholders})",
            (normalized, occurred_at, occurred_at, str(error or "")[:1000], *changed_ids),
        )
        updated_count = int(cursor.rowcount)
        for row in rows:
            action_type = (
                INVENTORY_ACTION
                if str(row["stable_source_id"] or "").startswith("ff_inventory:")
                else OVERHEAD_ACTION
            )
            duration_ms = _elapsed_ms(str(row["stage_started_at"] or ""), occurred_at)
            event_id = "ffwe_" + _digest(
                {
                    "action_type": action_type,
                    "identity": str(row["stable_source_id"] or ""),
                    "stage": "replay_economics",
                    "status": normalized,
                    "occurred_at": occurred_at,
                }
            )[:24]
            conn.execute(
                f"INSERT OR IGNORE INTO {EVENT_TABLE}(event_id,action_type,identity,stage,status,"
                "occurred_at,duration_ms,details_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    action_type,
                    str(row["stable_source_id"] or ""),
                    "replay_economics",
                    normalized,
                    occurred_at,
                    duration_ms,
                    _json({"queue_id": str(row["queue_id"] or "")}),
                ),
            )
        conn.commit()
    for row in rows:
        _emit_metric(
            action_type=(
                INVENTORY_ACTION
                if str(row["stable_source_id"] or "").startswith("ff_inventory:")
                else OVERHEAD_ACTION
            ),
            identity=str(row["stable_source_id"] or ""),
            stage="replay_economics",
            status=normalized,
            duration_ms=_elapsed_ms(str(row["stage_started_at"] or ""), occurred_at),
        )
    return updated_count


def _select_inventory_preview(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    request_id: str,
    source_sha256: str,
    business_date: str,
) -> sqlite3.Row | None:
    if preview_id:
        return conn.execute(
            f"SELECT * FROM {INVENTORY_TABLE} WHERE preview_id=?", (preview_id,)
        ).fetchone()
    if request_id:
        direct = conn.execute(
            f"SELECT * FROM {INVENTORY_TABLE} WHERE request_id=?", (request_id,)
        ).fetchone()
        if direct is not None:
            return direct
        return conn.execute(
            f"SELECT preview.* FROM {ALIAS_TABLE} alias JOIN {INVENTORY_TABLE} preview "
            "ON preview.preview_id=alias.preview_id "
            "WHERE alias.request_id=? AND alias.action_type='inventory'",
            (request_id,),
        ).fetchone()
    if source_sha256:
        return conn.execute(
            f"SELECT * FROM {INVENTORY_TABLE} WHERE source_sha256=? AND (?='' OR business_date=?) "
            "ORDER BY created_at DESC,preview_id DESC LIMIT 1",
            (source_sha256, business_date, business_date),
        ).fetchone()
    return conn.execute(
        f"SELECT * FROM {INVENTORY_TABLE} ORDER BY created_at DESC,preview_id DESC LIMIT 1"
    ).fetchone()


def _select_overhead_preview(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    request_id: str,
    document_id: str,
) -> sqlite3.Row | None:
    if preview_id:
        return conn.execute(
            f"SELECT * FROM {OVERHEAD_TABLE} WHERE preview_id=?", (preview_id,)
        ).fetchone()
    if request_id:
        direct = conn.execute(
            f"SELECT * FROM {OVERHEAD_TABLE} WHERE request_id=?", (request_id,)
        ).fetchone()
        if direct is not None:
            return direct
        return conn.execute(
            f"SELECT preview.* FROM {ALIAS_TABLE} alias JOIN {OVERHEAD_TABLE} preview "
            "ON preview.preview_id=alias.preview_id "
            "WHERE alias.request_id=? AND alias.action_type='overhead'",
            (request_id,),
        ).fetchone()
    if document_id:
        return conn.execute(
            f"SELECT * FROM {OVERHEAD_TABLE} WHERE document_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    return conn.execute(
        f"SELECT * FROM {OVERHEAD_TABLE} ORDER BY created_at DESC,preview_id DESC LIMIT 1"
    ).fetchone()


def _select_overhead_document(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    latest: bool,
) -> sqlite3.Row | None:
    if document_id:
        return conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_overhead_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
    if latest:
        return conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_overhead_documents "
            "ORDER BY created_at DESC,document_id DESC LIMIT 1"
        ).fetchone()
    return None


def _inventory_reconciliation(
    conn: sqlite3.Connection,
    preview: Mapping[str, Any],
) -> sqlite3.Row | None:
    reconciliation_id = str(preview["reconciliation_id"] or "")
    if reconciliation_id:
        row = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations "
            "WHERE reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations "
        "WHERE source_sha256=? AND business_date=? ORDER BY created_at DESC LIMIT 1",
        (str(preview["source_sha256"]), str(preview["business_date"])),
    ).fetchone()


def _replay_state(conn: sqlite3.Connection, *, stable_source_id: str) -> dict[str, Any]:
    if not stable_source_id:
        return {"status": "not_started", "queue_id": "", "error": ""}
    row = conn.execute(
        f"SELECT * FROM {QUEUE_TABLE} WHERE stable_source_id=? "
        "ORDER BY requested_at DESC,queue_id DESC LIMIT 1",
        (stable_source_id,),
    ).fetchone()
    if row is None:
        return {"status": "missing", "queue_id": "", "error": "Очередь пересчёта не найдена"}
    queue_status = str(row["status"] or "")
    economics_status = str(row["economics_status"] or "")
    economics_evidence = ""
    if queue_status == "complete" and economics_status != "complete":
        # Legacy rows predate the explicit economics marker.  A later durable
        # successful dependent phase is exact evidence that current functional
        # state was economically republished after this queue item completed.
        tables = {
            str(item[0])
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sheet_vitrina_v1_warehouse_update_phases" in tables:
            proof = conn.execute(
                """
                SELECT phase.run_id,phase.finished_at
                FROM sheet_vitrina_v1_warehouse_update_phases phase
                JOIN sheet_vitrina_v1_warehouse_update_runs run ON run.run_id=phase.run_id
                WHERE phase.phase_key='dependent_replay_economics'
                  AND phase.status='success' AND run.status='success'
                  AND COALESCE(phase.finished_at,'')>=COALESCE(?, '')
                ORDER BY phase.finished_at DESC LIMIT 1
                """,
                (str(row["finished_at"] or ""),),
            ).fetchone()
            if proof is not None:
                economics_status = "complete"
                economics_evidence = "warehouse_update_phase:" + str(proof["run_id"])
    if queue_status in {"queued", "running"}:
        public_status = queue_status
    elif queue_status == "complete" and economics_status == "complete":
        public_status = "complete"
    elif economics_status == "error":
        public_status = "error"
    else:
        public_status = "economics_pending"
    return {
        "status": public_status,
        "queue_status": queue_status,
        "economics_status": economics_status or "pending",
        "queue_id": str(row["queue_id"] or ""),
        "requested_at": str(row["requested_at"] or ""),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["economics_finished_at"] or row["finished_at"] or ""),
        "error": str(row["economics_error"] or row["error"] or ""),
        "evidence": economics_evidence or ("queue:" + str(row["queue_id"] or "")),
    }


def _inventory_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    documents = [dict(item) for item in manifest.get("documents") or []]
    changed = [
        dict(item)
        for item in manifest.get("per_sku") or []
        if str(item.get("before_quantity") or "0") != str(item.get("target_quantity") or "0")
    ]
    receipt = [item for item in documents if str(item.get("operation_type")) == "inventory_receipt"]
    writeoff = [item for item in documents if str(item.get("operation_type")) == "inventory_writeoff"]
    return {
        "before_total": str(manifest.get("before_total") or ""),
        "target_total": str(manifest.get("target_total") or ""),
        "inventory_quantity_delta": str(manifest.get("inventory_quantity_delta") or ""),
        "inventory_capital_delta_rub": str(manifest.get("inventory_capital_delta_rub") or ""),
        "target_sku_count": int(dict(manifest.get("invariants") or {}).get("target_sku_count") or 0),
        "changed_sku_count": len(changed),
        "changed_skus": changed[:50],
        "receipt_document_count": len(receipt),
        "receipt_line_count": sum(len(item.get("lines") or []) for item in receipt),
        "writeoff_document_count": len(writeoff),
        "writeoff_line_count": sum(len(item.get("lines") or []) for item in writeoff),
    }


def _inventory_document_public(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "document_id": str(row["reconciliation_id"] or ""),
        "reconciliation_id": str(row["reconciliation_id"] or ""),
        "status": str(row["status"] or ""),
        "operation_ids": _loads(row["operation_ids_json"], []),
        "created_by": str(row["created_by"] or ""),
        "created_at": str(row["created_at"] or ""),
        "readback": _loads(row["reconciliation_json"], {}),
    }


def _overhead_details(
    preview: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
    document: Mapping[str, Any] | None,
) -> dict[str, Any]:
    allocations = (
        _loads(document["allocations_json"], [])
        if document is not None
        else list(manifest.get("allocations") or [])
    )
    return {
        "business_date": str(
            (document["business_date"] if document is not None else "")
            or (preview["business_date"] if preview is not None else "")
            or manifest.get("business_date")
            or ""
        ),
        "amount_rub": str(
            (document["amount_rub"] if document is not None else "")
            or (preview["amount_rub"] if preview is not None else "")
            or manifest.get("amount_rub")
            or ""
        ),
        "reason": str(
            (document["reason"] if document is not None else "")
            or (preview["reason"] if preview is not None else "")
            or manifest.get("reason")
            or ""
        ),
        "denominator_quantity": str(
            (document["denominator_quantity"] if document is not None else "")
            or manifest.get("denominator_quantity")
            or ""
        ),
        "allocated_sku_count": len(allocations),
        "physical_quantity_unchanged": bool(
            dict(_loads(document["readback_json"], {}) if document is not None else {}).get(
                "physical_quantity_unchanged",
                bool(document is None),
            )
        ),
    }


def _overhead_document_public(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "document_id": str(row["document_id"] or ""),
        "operation_id": str(row["operation_id"] or ""),
        "status": str(row["status"] or ""),
        "created_by": str(row["created_by"] or ""),
        "created_at": str(row["created_at"] or ""),
        "readback": _loads(row["readback_json"], {}),
    }


def _inventory_validation_public(*, code: str, details: Any, business_date: str) -> dict[str, Any]:
    items = (
        [dict(item) for item in details or [] if isinstance(item, Mapping)]
        if isinstance(details, list)
        else []
    )
    mismatch = [item for item in items if str(item.get("code") or "") == "business_date_mismatch"]
    message = ""
    if code == "invalid_workbook_rows" and mismatch and len(mismatch) == len(items):
        actual_dates = sorted({str(item.get("actual") or "") for item in mismatch})
        if len(actual_dates) == 1:
            rows = sorted({int(item.get("row") or 0) for item in mismatch if int(item.get("row") or 0) > 0})
            message = (
                f"Дата в файле: {_date_ru(actual_dates[0])}; дата в форме: {_date_ru(business_date)}. "
                f"Исправьте колонку «Дата остатка» или выберите в форме дату {_date_ru(actual_dates[0])}. "
                f"Затронуты строки {_row_ranges(rows)} ({len(rows)} {_ru_rows(len(rows))})."
            )
    if not message:
        messages = {
            "invalid_workbook_headers": "Заголовки файла не соответствуют шаблону инвентаризации FF. Скачайте новый шаблон и перенесите значения без изменения колонок.",
            "invalid_workbook_rows": "В файле есть некорректные или повторяющиеся строки. Исправьте отмеченные строки и загрузите тот же файл снова.",
            "plan_blocked": "Проверка выявила блокеры. Исправьте указанные позиции и повторите загрузку.",
            "preview_processing_failed": "Проверка не завершилась. Данные сохранены; безопасно обновите страницу или повторите проверку позже.",
        }
        message = messages.get(code, "")
    examples = [
        {**item, "message_ru": _inventory_detail_ru(item)}
        for item in items[:8]
    ]
    return {
        "code": code,
        "message_ru": message,
        "affected_count": len(items),
        "examples": examples,
        "details": details,
        "details_truncated": False,
    }


def _inventory_detail_ru(item: Mapping[str, Any]) -> str:
    code = str(item.get("code") or "")
    messages = {
        "business_date_mismatch": "Дата остатка не совпадает с датой в форме",
        "invalid_business_date_cell": "Укажите дату остатка в формате ДД.ММ.ГГГГ",
        "barcode_must_be_text": "Штрихкод должен быть текстом без потери ведущих нулей",
        "invalid_barcode": "Проверьте значение штрихкода",
        "scientific_notation_barcode": "Штрихкод нельзя записывать в научной нотации",
        "fractional_barcode": "Штрихкод не может быть дробным",
        "invalid_nm_id": "Проверьте nmId",
        "unsafe_nm_id_cell": "nmId должен быть целым идентификатором",
        "scientific_notation_nm_id": "nmId нельзя записывать в научной нотации",
        "invalid_target_quantity": "Остаток FF должен быть целым неотрицательным числом",
        "scientific_notation_target_quantity": "Остаток FF нельзя записывать в научной нотации",
        "duplicate_sku": "Одна позиция указана в файле несколько раз",
        "unknown_sku": "Позиция не найдена в активной номенклатуре",
        "ambiguous_sku": "Позиция неоднозначно сопоставлена с номенклатурой",
        "identity_conflict": "nmId и штрихкод относятся к разным позициям",
    }
    if code in messages:
        return messages[code]
    field = str(item.get("field") or "").strip()
    return f"Проверьте значение в колонке «{field}»" if field else "Проверьте данные этой строки"


def _overhead_error_ru(code: str, details: Any) -> str:
    messages = {
        "invalid_business_date": "Проверьте дату учёта и повторите проверку.",
        "invalid_amount": "Укажите положительную сумму с точностью до копеек.",
        "reason_required": "Укажите основание накладных расходов.",
        "reason_too_long": "Сократите основание до 500 символов.",
        "positive_denominator_missing": "На выбранную дату нет положительного остатка FF. Выберите корректную дату.",
        "preview_processing_failed": "Проверка не завершилась. Данные сохранены; безопасно обновите страницу или повторите позже.",
    }
    return messages.get(code, str((details or {}).get("error") or "") if isinstance(details, Mapping) else "")


def _steps(state: str, replay: Mapping[str, Any]) -> list[dict[str, str]]:
    order = [
        ("accepted", "Файл/данные приняты сервером"),
        ("checked", "Проверка завершена"),
        ("ready", "Готово к проведению"),
        ("applied", "Документ проведён"),
        ("replay_complete", "Распределение/складской пересчёт завершён"),
    ]
    rank = {
        "accepted": 0,
        "processing": 0,
        "blocked": 1,
        "error": 1,
        "replay_error": 3,
        "ready": 2,
        "applied": 3,
        "replay_complete": 4,
    }.get(state, -1)
    result = []
    for index, (key, label) in enumerate(order):
        if index < rank or index == rank and state not in {"blocked", "error", "processing"}:
            status = "complete"
        elif key == "checked" and state in {"blocked", "error"}:
            status = "blocked" if state == "blocked" else "error"
        elif key == "accepted" and state == "processing":
            status = "complete"
        elif key == "checked" and state == "processing":
            status = "running"
        elif key == "replay_complete" and state in {"applied", "replay_error"}:
            status = "running" if replay.get("status") in {"queued", "running", "economics_pending"} else "error"
        else:
            status = "pending"
        result.append({"key": key, "label_ru": label, "status": status})
    return result


def _not_found(action_type: str) -> dict[str, Any]:
    return {
        "contract_name": "ff_document_workflow_v1",
        "action_type": action_type,
        "state": "not_found",
        "confirm_allowed": False,
        "steps": _steps("not_found", {}),
    }


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(normalized):
        raise ValueError("request_id отсутствует или имеет неверный формат; обновите страницу и повторите")
    return normalized


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    definitions: Mapping[str, str],
) -> None:
    existing = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _date_ru(value: str) -> str:
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(value or "—")


def _elapsed_ms(started_at: str, finished_at: str) -> int:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        return max(0, int((finish - start).total_seconds() * 1000))
    except ValueError:
        return 0


def _row_ranges(rows: list[int]) -> str:
    if not rows:
        return "—"
    ranges: list[str] = []
    start = previous = rows[0]
    for value in rows[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return ", ".join(ranges)


def _ru_rows(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return "строк"
    last = count % 10
    return "строка" if last == 1 else "строки" if last in {2, 3, 4} else "строк"


def _emit_metric(
    *,
    action_type: str,
    identity: str,
    stage: str,
    status: str,
    duration_ms: int,
) -> None:
    budget_ms = {
        "file_accepted": 5_000,
        "data_accepted": 5_000,
        "document_committed": 5_000,
        "validation": 120_000,
        "replay_economics": 300_000,
    }.get(stage, 120_000)
    print(
        _json(
            {
                "event": "ff_document_workflow_stage",
                "action_type": action_type,
                "identity": identity,
                "stage": stage,
                "status": status,
                "duration_ms": int(duration_ms),
                "slo_budget_ms": budget_ms,
                "slo_exceeded": int(duration_ms) > budget_ms,
            }
        ),
        file=sys.stderr,
        flush=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _connect(path: Any, *, query_only: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro" if query_only else str(path)
    conn = sqlite3.connect(uri, uri=query_only, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if query_only:
        conn.execute("PRAGMA query_only=ON")
    return conn
